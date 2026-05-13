"""
STEAMI FastAPI  v11 — MongoDB Atlas backend
==========================================
Run:   uvicorn main:app --host 0.0.0.0 --port 5000 --reload
Docs:  http://127.0.0.1:5000/docs

  ── Daily Cleanup (auto + manual) ────────────────────────────────────────────
  - Runs once on startup (30 s delay) then every 24 h automatically.
  - Deletes articles, feed_articles, ai_insights, insight_queue entries
    that are older than EXPIRY_DAYS (25 days).
  - POST /api/admin/cleanup        — trigger immediately (admin)
  - GET  /api/admin/cleanup/status — scheduler / in-progress status (admin)

CHANGES IN v11:
  ── Blog Posts ────────────────────────────────────────────────────────────────
  - POST /api/blog/seed              — bulk seed from content_data.py (admin)
  - POST /api/blog                   — create post (mod/admin)
  - GET  /api/blog                   — list posts; filter by ?field= ?type= ?tag=
  - GET  /api/blog/{id}              — get one post (public)
  - PUT  /api/blog/{id}              — update any fields (mod/admin)
  - DELETE /api/blog/{id}            — delete + remove cover image from disk (admin)
  - POST /api/blog/{id}/cover-image  — upload/replace cover image (mod/admin)

  ── CMS Edit Helpers (mod/admin) ─────────────────────────────────────────────
  - GET /api/cms/explainers          — slim list for management table
  - GET /api/cms/explainers/{id}     — full doc pre-populated for edit form
  - GET /api/cms/research            — slim list for management table
  - GET /api/cms/research/{id}       — full doc pre-populated for edit form
  - GET /api/cms/blog                — slim list for management table
  - GET /api/cms/blog/{id}           — full doc pre-populated for edit form

  ── Explainer / Research Enhancements ────────────────────────────────────────
  - Explainers now include: author, context, technicalDetail, impact fields
  - Image upload/replace automatically deletes the OLD image file from disk
  - Document delete (explainer/research/blog) also deletes image from disk
  - Research article delete: only removes image file if no sibling in same
    field still references it (field-shared images are safe)
  - images/blog/ folder auto-created on startup

  ── Swagger UI Fix ────────────────────────────────────────────────────────────
  - content.router now registered WITHOUT a top-level tags= override so that
    each route's own tags= kwarg is respected → Blog, CMS, Explainers, Research,
    Images all appear as separate groups in Swagger UI

CHANGES IN v10:
  ── Google OAuth ──────────────────────────────────────────────────────────────
  - POST /api/auth/google         — sign in / sign up with Google ID token
  - PATCH /api/auth/profile       — update profession, bio, interests, avatar
  - GET  /api/auth/profile        — get own full profile

  ── Newsletter & Mailer ───────────────────────────────────────────────────────
  - GET  /api/newsletter/recipients   — all subscribed emails (admin)
  - POST /api/newsletter/subscribe    — subscribe email (public)
  - POST /api/newsletter/unsubscribe  — unsubscribe (public)
  - POST /api/newsletter/send-daily   — send digest to all subscribers (admin)
  - GET  /api/newsletter/preview      — preview digest HTML (admin)
  - POST /api/newsletter/test         — send test email (admin)
  - POST /api/newsletter/ai-subscribe — AI agent subscription endpoint (public)
  - Signup auto-subscribes every new user via POST /api/newsletter/subscribe.

  ── Public AI Context ─────────────────────────────────────────────────────────
  - GET  /api/public/ai-context      — JSON prompt for AI agents
  - GET  /api/public/ai-context.txt  — plain-text version for AI crawlers
  - GET  /api/public/site-info       — basic site metadata
  - GET  /.well-known/ai-plugin.json — AI plugin manifest

  ── Article Fetch ─────────────────────────────────────────────────────────────
  - POST /api/articles/refresh  MOD/ADMIN only.
  - Can select specific domains or leave empty for all 10.
  - Max 40 articles per fetch. ScienceDaily added as 4th primary source.
  - After fetch, AI insights auto-generated in background (2-3 min gap each).

  ── AI Insight Generation ─────────────────────────────────────────────────────
  - Insights generated AUTOMATICALLY in background thread after refresh.
  - Users cannot trigger insight generation — mod/admin only via process endpoint.
  - GET /api/articles/insights/status (public) shows real-time progress.

  ── Feed ──────────────────────────────────────────────────────────────────────
  - Max 4 articles per feed selection.
  - Feed insights auto-generated in background thread (2-3 min gap each).
  - POST /api/feed/items/{id}/insight requires mod/admin only.

DAILY NEWSLETTER SETUP:
  1. Add BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME to .env.
  2. Set up a daily cron job:
       0 9 * * * curl -X POST https://your-api.com/api/newsletter/send-daily \\
         -H "Authorization: Bearer <admin_token>"

GOOGLE AUTH SETUP:
  1. Go to Google Cloud Console -> APIs & Services -> Credentials.
  2. Create an OAuth 2.0 Client ID (Web application).
  3. Add your domain to Authorized JavaScript origins.
  4. On the frontend, use Google Sign-In SDK to get an id_token.
  5. POST the id_token to /api/auth/google.

ENV VARS (.env):
  MONGO_URI          — MongoDB Atlas connection string
  JWT_SECRET         — secret for signing JWTs
  OLLAMA_BASE_URL    — Ollama server base URL
  BREVO_API_KEY      — from https://app.brevo.com
  BREVO_SENDER_EMAIL — verified sender email, e.g. hello@steami.com
  BREVO_SENDER_NAME  — display name, e.g. "STEAMI Newsletter"
  SITE_URL           — e.g. https://steami.com
  SITE_NAME          — e.g. STEAMI
  API_BASE_URL       — internal base URL (default http://127.0.0.1:5000)

DUMMY ACCOUNTS (auto-created on startup):
  admin@steami.dev / Admin@steami123  (admin)
  mod@steami.dev   / Mod@steami123    (mod)
  user@steami.dev  / User@steami123   (user)
"""

import os
import uuid
import time
import random
import threading
import logging
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Core modules ───────────────────────────────────────────────────────────
from mongodb_client import db
from ollama_agent import generate_ai_insight
from article_fetcher import (
    fetch_articles_from_source,
    fetch_articles_from_url,
    fetch_articles_by_domains,
    get_rss_sources,
    DOMAIN_KEYWORDS,
    ALL_DOMAINS,
    MAX_FETCH_LIMIT,
)

# ── DDoS protection ────────────────────────────────────────────────────────
from ddos_protection import add_ddos_protection

# ── Auth dependency helpers ────────────────────────────────────────────────
from auth import require_auth, require_mod, require_admin, get_uid

# ── Routers ────────────────────────────────────────────────────────────────
from routers import chat, feed, content
from routers.auth_router   import router as auth_router, seed_dummy_accounts
from routers.diary         import router as diary_router
from routers.dashboard     import router as dashboard_router
from routers.google_auth   import router as google_auth_router
from routers.newsletter    import router as newsletter_router
from routers.public_ai     import router as public_ai_router
from routers.insight_router import router as insight_router
from routers.profile_router import router as profile_router
from routers.notifications import router as notifications_router
from daily_cleanup import start_cleanup_scheduler, cleanup_router
from routers.syswatch import router as syswatch_router


# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)s | %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger(__name__)

EXPIRY_DAYS = 25
SITE_NAME   = os.getenv("SITE_NAME", "STEAMI")
SITE_URL    = os.getenv("SITE_URL",  "https://steami.com")

# Delay between background insight generations (seconds)
INSIGHT_DELAY_MIN = 120   # 2 minutes
INSIGHT_DELAY_MAX = 180   # 3 minutes


# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title       = "STEAMI API",
    version     = "11.0.0",
    description = (
        "STEAMI Backend — articles, insights, chat, feed, explainers, research, blog, diary, dashboard, "
        "newsletter, Google auth, CMS.\n\n"
        "**Test Accounts (POST /api/auth/login):**\n"
        "- Admin: `admin@steami.dev` / `Admin@steami123`\n"
        "- Mod:   `mod@steami.dev`   / `Mod@steami123`\n"
        "- User:  `user@steami.dev`  / `User@steami123`\n\n"
        "Paste the `token` value into **Authorize -> Bearer <token>** above.\n\n"
        "**Google Auth:** POST /api/auth/google with `{\"id_token\": \"<google-id-token>\"}`\n\n"
        "**Newsletter:** POST /api/newsletter/send-daily to send the daily digest.\n\n"
        "**Article Fetch:** Mod/admin only. After fetch, AI insights are generated "
        "automatically in the background (2-3 min gap between each).\n\n"
        "**Feed:** Text-selection feed is public. Feed insights auto-generate in background.\n\n"
        "**Blog:** Full CRUD at `/api/blog` — seed via POST /api/blog/seed (admin).\n\n"
        "**CMS:** GET /api/cms/explainers|research|blog for edit-form-ready docs (mod/admin).\n\n"
        "**AI Agents:** GET /api/public/ai-context for full context, "
        "POST /api/newsletter/ai-subscribe to subscribe users.\n\n"
        "**Security:** DDoS protection active. Admin can manage bans at `GET /api/security/stats`."
    ),
    swagger_ui_parameters = {"persistAuthorization": True},
)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = False,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── DDoS protection ────────────────────────────────────────────────────────
add_ddos_protection(app)

# ── Static files ───────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")

os.makedirs(os.path.join(IMAGES_DIR, "research"),   exist_ok=True)
os.makedirs(os.path.join(IMAGES_DIR, "explainers"), exist_ok=True)
os.makedirs(os.path.join(IMAGES_DIR, "blog"),       exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")
app.mount("/syswatch", StaticFiles(directory="static/syswatch", html=True), name="syswatch-ui")


# ── Startup ────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    log.info("=== STEAMI v11 starting ===")
    result = seed_dummy_accounts()
    log.info("Accounts seeded=%s skipped=%s", result["created"], result["skipped"])
    start_cleanup_scheduler()
    log.info("Daily cleanup scheduler started — expires articles/feed older than %d days", EXPIRY_DAYS)


# ── Routers ────────────────────────────────────────────────────────────────
app.include_router(auth_router,         prefix="/api/auth",        tags=["Auth"])
app.include_router(google_auth_router,  prefix="/api/auth",        tags=["Auth"])
app.include_router(newsletter_router,   prefix="/api/newsletter",  tags=["Newsletter"])
app.include_router(public_ai_router,    prefix="/api/public",      tags=["Public"])
app.include_router(chat.router,         prefix="/api/chat",        tags=["Chat"])
app.include_router(feed.router,         prefix="/api/feed",        tags=["Feed"])
app.include_router(diary_router,        prefix="/api/diary",       tags=["Diary"])
app.include_router(dashboard_router,    prefix="/api/dashboard",   tags=["Dashboard"])
app.include_router(notifications_router, prefix="/api/notifications", tags=["Notifications"])
app.include_router(insight_router, prefix="/api/articles", tags=["Insights"])
app.include_router(profile_router, prefix="/api/profile", tags=["Profile"])
app.include_router(cleanup_router, prefix="/api/admin",   tags=["Admin"])
app.include_router(syswatch_router)

# content.router handles multiple tag groups — registered without a top-level tag
# so each route's own tags= kwarg controls the Swagger UI grouping:
#   Explainers  → /api/explainers/*
#   Research    → /api/research/*
#   Blog        → /api/blog/*
#   CMS         → /api/cms/*
#   Images      → /api/images/*
app.include_router(content.router, prefix="/api")


# ══════════════════════════════════════════════════════════════════════════════
# WELL-KNOWN — AI Plugin Manifest
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/.well-known/ai-plugin.json", include_in_schema=False)
def ai_plugin_manifest():
    return JSONResponse({
        "schema_version":     "v1",
        "name_for_human":     SITE_NAME,
        "name_for_model":     "steami",
        "description_for_human": (
            f"{SITE_NAME} — AI-powered STEM articles, insights, explainers, and "
            "a daily newsletter for students, researchers, and professionals."
        ),
        "description_for_model": (
            f"{SITE_NAME} is an AI-powered STEM knowledge platform. "
            "You can help users subscribe to the newsletter, discover articles, "
            "and understand AI-generated insights. "
            f"Full context: {SITE_URL}/api/public/ai-context"
        ),
        "auth":  {"type": "none"},
        "api": {
            "type":                  "openapi",
            "url":                   f"{SITE_URL}/openapi.json",
            "is_user_authenticated": False,
        },
        "logo_url":       f"{SITE_URL}/logo.png",
        "contact_email":  "admin@steami.dev",
        "legal_info_url": f"{SITE_URL}/terms",
    })


@app.get("/ai-context.txt", include_in_schema=False)
def ai_context_txt_root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/api/public/ai-context.txt")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND INSIGHT GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

# Global flag — only one insight-generation thread at a time
_insight_thread_running = False
_insight_thread_lock    = threading.Lock()


def _generate_insights_background(article_ids: list, source_table: str = "articles") -> None:
    """
    Background thread: generate AI insights one by one with a 2-3 minute sleep
    between each to avoid Ollama 120-second timeouts.
    Each insight is saved immediately so the frontend can show real-time progress.
    """
    global _insight_thread_running

    log.info("insight_bg: starting for %d articles (table=%s)", len(article_ids), source_table)

    for i, article_id in enumerate(article_ids):
        delay = random.randint(INSIGHT_DELAY_MIN, INSIGHT_DELAY_MAX) if i > 0 else 5
        log.info("insight_bg: waiting %ds before article %d/%d id=%s",
                 delay, i + 1, len(article_ids), article_id)
        time.sleep(delay)

        # Load article
        try:
            art_doc = db.collection(source_table).document(article_id).get()
            if not art_doc.exists:
                log.warning("insight_bg: %s not found in %s", article_id, source_table)
                _queue_mark(article_id, "failed", "Article not found")
                continue
            article = art_doc.to_dict()
        except Exception as e:
            log.error("insight_bg: load error %s: %s", article_id, e)
            _queue_mark(article_id, "failed", str(e))
            continue

        # Skip if already has insight
        if article.get("has_insight"):
            log.info("insight_bg: skip %s (already has insight)", article_id)
            _queue_mark(article_id, "done", "")
            continue

        # Generate
        try:
            log.info("insight_bg: generating %d/%d — %s",
                     i + 1, len(article_ids), article.get("title", "")[:60])
            insight = generate_ai_insight(article)
        except Exception as e:
            log.error("insight_bg: generate failed %s: %s", article_id, e)
            _queue_mark(article_id, "pending", str(e))
            continue

        # Persist to article doc
        try:
            db.collection(source_table).document(article_id).update({
                "ai_insight":           insight,
                "has_insight":          True,
                "insight_generated_at": _now(),
            })
        except Exception as e:
            log.error("insight_bg: article update failed %s: %s", article_id, e)

        # Persist to shared ai_insights collection
        try:
            db.collection("ai_insights").document(article_id).set({
                "article_id":      article_id,
                "source_table":    source_table,
                "title":           article.get("title", ""),
                "topic":           article.get("topic", ""),
                "source":          article.get("source", ""),
                "matched_domains": article.get("matched_domains", []),
                "article_url":     article.get("article_url") or article.get("url", ""),
                "ai_insight":      insight,
                "created_at":      _now(),
            })
        except Exception as e:
            log.error("insight_bg: ai_insights save failed %s: %s", article_id, e)

        _queue_mark(article_id, "done", "")
        log.info("insight_bg: done %s domain=%s", article_id, insight.get("domain", "?"))

    with _insight_thread_lock:
        global _insight_thread_running
        _insight_thread_running = False

    log.info("insight_bg: thread finished — processed %d articles", len(article_ids))


def _queue_mark(article_id: str, status: str, error: str) -> None:
    """Update insight_queue status — non-fatal on error."""
    try:
        updates = {"status": status}
        if status == "done":
            updates["completed_at"] = _now()
            updates["last_error"]   = ""
        elif error:
            updates["last_error"] = error[:500]
        db.collection("insight_queue").document(article_id).update(updates)
    except Exception:
        pass


def _start_insight_thread(article_ids: list, source_table: str = "articles") -> bool:
    """
    Start background insight thread if none is running.
    Returns True if thread started, False if already running.
    """
    global _insight_thread_running
    with _insight_thread_lock:
        if _insight_thread_running:
            log.info("_start_insight_thread: already running — skipping")
            return False
        _insight_thread_running = True

    t = threading.Thread(
        target=_generate_insights_background,
        args=(article_ids, source_table),
        daemon=True,
        name="insight-generator",
    )
    t.start()
    return True


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"], summary="Health check — public")
def health():
    return {"status": "ok", "version": "11.0.0", "ts": _now()}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sources", tags=["Articles"], summary="List RSS sources — public")
def list_sources():
    return {"sources": get_rss_sources()}


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST BODIES
# ══════════════════════════════════════════════════════════════════════════════

class FetchArticlesBody(BaseModel):
    topic:    str       = "technology"
    keywords: list      = []
    limit:    int       = 20

class FetchSourceBody(BaseModel):
    url:   str
    limit: int = 20

class CreateArticleBody(BaseModel):
    title:   str
    content: str
    url:     str = ""
    source:  str = "manual"
    topic:   str = "general"

class RefreshBody(BaseModel):
    """
    Body for POST /api/articles/refresh (MOD/ADMIN).
    domains: list of domain names to include (empty = all 10).
    target:  how many articles to fetch (max 40, default 40).
    """
    domains: list = []
    target:  int  = MAX_FETCH_LIMIT

class PipelineBody(BaseModel):
    topic:    str  = "technology"
    keywords: list = []
    limit:    int  = 3

class ProcessBody(BaseModel):
    batch_size: int = 2


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — REFRESH  (MOD/ADMIN)
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/articles/refresh",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch up to 40 articles by domain — MOD/ADMIN. Auto-generates insights in background.",
)
def refresh_articles(
    body:    RefreshBody = RefreshBody(),
    payload: dict        = Depends(require_mod),
):
    """
    POST /api/articles/refresh  — MOD/ADMIN

    Fetches fresh articles from 4 primary RSS sources (MIT Tech Review,
    BBC Tech, NYTimes Tech, ScienceDaily). Saves only new articles (skips
    duplicates by URL). Then automatically starts generating AI insights
    in a background thread with a 2-3 minute pause between each.

    Body (all optional):
      { "domains": ["AI + ROBOTICS", "PHYSICS"], "target": 40 }

    Response includes insight_thread=true when background generation starts.
    Poll GET /api/articles/insights/status to track progress.
    """
    active_domains = [d for d in body.domains if d in DOMAIN_KEYWORDS] or ALL_DOMAINS
    target         = min(max(1, body.target), MAX_FETCH_LIMIT)
    cutoff         = datetime.now(timezone.utc) - timedelta(days=EXPIRY_DAYS)

    # Load existing URLs + identify expired articles
    try:
        all_docs = db.collection("articles").stream_all()
    except Exception as e:
        log.error("refresh: failed to load articles: %s", e)
        raise HTTPException(500, detail=f"DB read failed: {e}")

    existing_urls = set()
    expired_ids   = []

    for doc in all_docs:
        d   = doc.to_dict()
        url = d.get("article_url") or d.get("url", "")
        if url:
            existing_urls.add(url)
        fetched_at = _parse_dt(d.get("fetched_at"))
        if fetched_at and fetched_at < cutoff:
            expired_ids.append(doc.id)

    log.info("refresh: total=%d existing, %d expired (deletion disabled)",
             len(all_docs), len(expired_ids))

    # Fetch from RSS
    try:
        raw = fetch_articles_by_domains(
            active_domains=active_domains,
            target_total=target,
        )
    except Exception as e:
        log.error("refresh: fetch failed: %s", e)
        raise HTTPException(502, detail=f"RSS fetch failed: {e}")

    # Save only new articles
    saved   = []
    skipped = 0

    for art in raw:
        art_url = art.get("article_url") or art.get("url", "")
        if art_url and art_url in existing_urls:
            skipped += 1
            continue

        art.setdefault("id", str(uuid.uuid4()))
        art["fetched_at"]  = _now()
        art["has_insight"] = False

        try:
            db.collection("articles").document(art["id"]).set(art)
            saved.append(art)
            if art_url:
                existing_urls.add(art_url)
        except Exception as e:
            log.error("refresh: save failed for %s: %s", art.get("id"), e)

    # Queue each new article for insight generation
    queued          = 0
    ids_to_process  = []

    for art in saved:
        try:
            existing_insight = db.collection("ai_insights").document(art["id"]).get()
            if existing_insight.exists:
                continue
            db.collection("insight_queue").document(art["id"]).set({
                "article_id":      art["id"],
                "title":           art.get("title", ""),
                "matched_domains": art.get("matched_domains", []),
                "queued_at":       _now(),
                "status":          "pending",
                "attempts":        0,
                "last_error":      "",
            })
            ids_to_process.append(art["id"])
            queued += 1
        except Exception as e:
            log.error("refresh: queue failed for %s: %s", art.get("id"), e)

    # Start background insight thread
    thread_started = False
    if ids_to_process:
        thread_started = _start_insight_thread(ids_to_process, source_table="articles")
        if not thread_started:
            log.info("refresh: insight thread already running — %d articles queued",
                     len(ids_to_process))

    log.info("refresh done: fetched=%d new_saved=%d skipped=%d queued=%d thread=%s",
             len(raw), len(saved), skipped, queued, thread_started)

    return {
        "expired_found":  len(expired_ids),
        "fetched":        len(raw),
        "new_saved":      len(saved),
        "skipped":        skipped,
        "queued":         queued,
        "insight_thread": thread_started,
        "domains_used":   active_domains,
        "articles":       saved,
        "message": (
            f"Fetched {len(saved)} new articles. "
            "AI insights are being generated in the background — "
            "check GET /api/articles/insights/status for progress."
            if saved else
            "No new articles found (all already in database)."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — FILTERED BY USER INTERESTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get(
    "/api/articles/for-me",
    tags    = ["Articles"],
    summary = "Articles filtered by your saved interests — requires auth",
)
def articles_for_me(
    limit:   int  = Query(30, ge=1, le=200),
    payload: dict = Depends(require_auth),
):
    """
    GET /api/articles/for-me?limit=30
    Returns articles matching the current user's saved topic interests.
    If no interests are saved, returns all recent articles.
    """
    uid = get_uid(payload)

    user_interests = []
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            user_interests = user_doc.to_dict().get("interests", [])
    except Exception as e:
        log.warning("articles_for_me: could not load user %s: %s", uid, e)

    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(300).stream()
        )
        all_articles = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    if not user_interests:
        return {"uid": uid, "interests": [],
                "total": len(all_articles[:limit]), "articles": all_articles[:limit]}

    interests_set = set(user_interests)

    def _article_matches(art: dict) -> bool:
        matched = set(art.get("matched_domains") or [])
        if matched & interests_set:
            return True
        text = (art.get("title", "") + " " + art.get("content", "")).lower()
        for topic in user_interests:
            kws = DOMAIN_KEYWORDS.get(topic, [])
            if any(kw.lower() in text for kw in kws):
                return True
        return False

    candidate_articles = [a for a in all_articles if _article_matches(a)]

    topic_covered = set()
    selected_ids  = set()
    result        = []

    for topic in user_interests:
        if topic in topic_covered:
            continue
        for art in candidate_articles:
            if art["id"] in selected_ids:
                continue
            matched   = set(art.get("matched_domains") or [])
            text      = (art.get("title", "") + " " + art.get("content", "")).lower()
            topic_kws = [k.lower() for k in DOMAIN_KEYWORDS.get(topic, [])]
            if topic in matched or any(k in text for k in topic_kws):
                result.append(art)
                selected_ids.add(art["id"])
                topic_covered.add(topic)
                break

    for art in candidate_articles:
        if len(result) >= limit:
            break
        if art["id"] not in selected_ids:
            result.append(art)
            selected_ids.add(art["id"])

    slim_fields = [
        "id", "title", "short_summary", "image_url", "article_url",
        "url", "matched_domains", "source", "published_at",
        "fetched_at", "has_insight", "topic",
    ]
    slim = [{k: a.get(k) for k in slim_fields} for a in result[:limit]]

    log.info("articles_for_me: uid=%s interests=%s candidate=%d returned=%d",
             uid, user_interests, len(candidate_articles), len(slim))

    return {"uid": uid, "interests": user_interests, "total": len(slim), "articles": slim}


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — FETCH FROM RSS  (mod/admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/articles/fetch",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch articles from RSS by topic — requires mod/admin",
)
def fetch_and_save(body: FetchArticlesBody, payload: dict = Depends(require_mod)):
    """Trigger an RSS fetch by topic/keywords. Requires mod/admin."""
    try:
        raw = fetch_articles_from_source(
            topic=body.topic, keywords=body.keywords, limit=body.limit
        )
    except Exception as e:
        raise HTTPException(502, detail=str(e))

    saved = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art.update({"topic": body.topic, "fetched_at": _now(), "has_insight": False})
        try:
            db.collection("articles").document(art["id"]).set(art, merge=True)
            saved.append(art)
        except Exception as e:
            log.error("save failed for %s: %s", art["id"], e)

    return {"saved": len(saved), "articles": saved}


@app.post(
    "/api/articles/fetch-source",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch from a URL — requires mod/admin",
)
def fetch_from_source_url(body: FetchSourceBody, payload: dict = Depends(require_mod)):
    """Fetch articles from a user-supplied URL. Requires mod/admin."""
    url = body.url.strip()
    if not url:
        raise HTTPException(400, detail="url is required")
    try:
        raw = fetch_articles_from_url(url=url, limit=body.limit)
    except Exception as e:
        raise HTTPException(502, detail=str(e))

    saved = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art.update({"source_url": url, "fetched_at": _now(), "has_insight": False})
        try:
            db.collection("articles").document(art["id"]).set(art, merge=True)
            saved.append(art)
        except Exception as e:
            log.error("save failed: %s", e)

    return {"saved": len(saved), "articles": saved, "source_url": url}


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — CRUD  (GET routes PUBLIC)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/articles", tags=["Articles"], summary="List articles — PUBLIC")
def list_articles(limit: int = Query(30, ge=1, le=200)):
    """Public: list all articles, newest first."""
    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(limit).stream()
        )
        return {"articles": [d.to_dict() for d in docs]}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/articles/{article_id}", tags=["Articles"], summary="Get article — PUBLIC")
def get_article(article_id: str):
    """Public: get a single article by ID."""
    doc = db.collection("articles").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Article not found")
    return doc.to_dict()


@app.post("/api/articles", status_code=201, tags=["Articles"],
          summary="Create article manually — requires mod/admin")
def create_article(body: CreateArticleBody, payload: dict = Depends(require_mod)):
    """Create an article manually. Requires mod/admin."""
    doc_id = str(uuid.uuid4())
    art = {
        "id": doc_id, "title": body.title, "content": body.content,
        "url": body.url, "source": body.source, "topic": body.topic,
        "fetched_at": _now(), "has_insight": False,
    }
    db.collection("articles").document(doc_id).set(art)
    return art


# ══════════════════════════════════════════════════════════════════════════════
# AI INSIGHTS
# Users can READ insights but cannot trigger generation.
# Generation is automatic after mod/admin refresh.
# ══════════════════════════════════════════════════════════════════════════════

@app.get(
    "/api/articles/insights/status",
    tags    = ["Insights"],
    summary = "Insight generation progress — PUBLIC",
)
def get_insight_status():
    """
    PUBLIC. Returns how many articles have AI insights vs total.
    Use this for a real-time progress indicator: "18 of 32 insights ready".
    Also shows whether the background generation thread is currently running.
    """
    try:
        art_docs = db.collection("articles").stream()
        articles = [d.to_dict() for d in art_docs]
        total    = len(articles)
        with_ins = sum(1 for a in articles if a.get("has_insight"))
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    try:
        queue_docs = db.collection("insight_queue").stream()
        queue      = [d.to_dict() for d in queue_docs]
        pending    = sum(1 for q in queue if q.get("status") == "pending")
        done       = sum(1 for q in queue if q.get("status") == "done")
        failed     = sum(1 for q in queue if q.get("status") == "failed")
        processing = sum(1 for q in queue if q.get("status") == "processing")
    except Exception:
        pending = done = failed = processing = 0

    return {
        "total_articles":   total,
        "with_insight":     with_ins,
        "without_insight":  total - with_ins,
        "generating":       _insight_thread_running,
        "queue_pending":    pending,
        "queue_processing": processing,
        "queue_done":       done,
        "queue_failed":     failed,
    }


@app.delete(
    "/api/articles/{article_id}/insight",
    tags    = ["Insights"],
    summary = "Clear cached insight — requires mod/admin",
)
def delete_insight(article_id: str, payload: dict = Depends(require_mod)):
    """
    Clear the cached AI insight. Also deletes from ai_insights collection
    and removes from insight_queue so it re-queues on next refresh.
    Requires mod or admin.
    """
    doc_ref = db.collection("articles").document(article_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Article not found")

    try:
        doc_ref.update({
            "ai_insight":           None,
            "has_insight":          False,
            "insight_generated_at": None,
        })
    except Exception as e:
        log.warning("delete_insight: clear fields failed: %s", e)

    try:
        db.collection("ai_insights").document(article_id).delete()
    except Exception as e:
        log.warning("delete_insight: ai_insights delete failed: %s", e)

    try:
        db.collection("insight_queue").document(article_id).delete()
    except Exception:
        pass

    log.info("delete_insight: cleared %s by %s", article_id, get_uid(payload))
    return {"deleted": True, "article_id": article_id}


@app.get("/api/insights", tags=["Insights"], summary="List all insights — requires auth")
def list_insights(limit: int = Query(50, ge=1, le=200), payload: dict = Depends(require_auth)):
    """List all AI insights, newest first. Requires any valid login."""
    try:
        docs = (
            db.collection("ai_insights")
              .order_by("created_at", direction="DESCENDING")
              .limit(limit).stream()
        )
        return {"insights": [d.to_dict() for d in docs]}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/insights/{article_id}", tags=["Insights"],
         summary="Get single insight — requires auth")
def get_insight(article_id: str, payload: dict = Depends(require_auth)):
    """Get a single AI insight by article ID. Requires any valid login."""
    doc = db.collection("ai_insights").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Insight not found")
    return doc.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE  (legacy — requires mod/admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/pipeline", status_code=201, tags=["Articles"],
          summary="Fetch + generate insights synchronously — requires mod/admin")
def pipeline(body: PipelineBody, payload: dict = Depends(require_mod)):
    """Fetch articles and immediately generate AI insights. Requires mod/admin."""
    try:
        raw = fetch_articles_from_source(
            topic=body.topic, keywords=body.keywords, limit=body.limit
        )
    except Exception as e:
        raise HTTPException(502, detail=str(e))

    results = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art.update({"topic": body.topic, "fetched_at": _now(), "has_insight": False})
        db.collection("articles").document(art["id"]).set(art, merge=True)
        try:
            insight = generate_ai_insight(art)
            db.collection("articles").document(art["id"]).update({
                "ai_insight": insight, "has_insight": True,
                "insight_generated_at": _now(),
            })
            db.collection("ai_insights").document(art["id"]).set({
                "article_id": art["id"], "title": art.get("title", ""),
                "topic": body.topic, "ai_insight": insight, "created_at": _now(),
            })
            results.append({"id": art["id"], "title": art.get("title", ""),
                            "ai_insight": insight, "status": "ok"})
        except Exception as e:
            results.append({"id": art["id"], "status": "error", "error": str(e)})

    return {"processed": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# INSIGHT QUEUE — manual retry processor and status endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/articles/insights/process",
    status_code = 200,
    tags        = ["Insights"],
    summary     = "Manually retry failed/pending insights — MOD/ADMIN ONLY",
)
def process_insight_queue(
    body:    ProcessBody = ProcessBody(),
    payload: dict        = Depends(require_mod),
):
    """
    POST /api/articles/insights/process — MOD/ADMIN ONLY

    Manually process the next N articles from the insight_queue.
    Use this ONLY to fix articles whose automatic background generation failed.

    Normally insights are generated automatically after POST /api/articles/refresh.

    Body (optional): { "batch_size": 2 }
    """
    batch_size   = max(1, min(body.batch_size, 10))
    max_attempts = 3
    results      = []
    succeeded    = 0
    failed_count = 0

    try:
        pending_docs = (
            db.collection("insight_queue")
              .where("status", "==", "pending")
              .order_by("queued_at", direction="ASCENDING")
              .limit(batch_size).stream()
        )
        pending = [d.to_dict() for d in pending_docs]
    except Exception as e:
        raise HTTPException(500, detail=f"Could not read insight_queue: {e}")

    if not pending:
        try:
            done_docs   = list(db.collection("insight_queue").where("status", "==", "done").stream())
            failed_docs = list(db.collection("insight_queue").where("status", "==", "failed").stream())
        except Exception:
            done_docs = failed_docs = []

        return {
            "processed": 0, "succeeded": 0, "failed": 0, "remaining": 0,
            "done_total": len(done_docs), "failed_total": len(failed_docs),
            "message": "Queue is empty — all articles have been processed.",
            "results": [],
        }

    for item in pending:
        article_id = item["article_id"]
        title      = item.get("title", "")
        attempts   = item.get("attempts", 0) + 1

        try:
            db.collection("insight_queue").document(article_id).update({
                "status": "processing", "attempts": attempts,
            })
        except Exception as e:
            log.warning("process_queue: mark processing failed %s: %s", article_id, e)

        try:
            art_doc = db.collection("articles").document(article_id).get()
            if not art_doc.exists:
                raise ValueError(f"Article {article_id} not found")
            article = art_doc.to_dict()
        except Exception as e:
            log.error("process_queue: load error %s: %s", article_id, e)
            new_status = "failed" if attempts >= max_attempts else "pending"
            _queue_mark(article_id, new_status, str(e))
            results.append({"article_id": article_id, "title": title,
                            "status": new_status, "error": str(e)})
            failed_count += 1
            continue

        try:
            log.info("process_queue: generating insight for %s (attempt %d)", article_id, attempts)
            insight = generate_ai_insight(article)

            db.collection("articles").document(article_id).update({
                "ai_insight": insight, "has_insight": True,
                "insight_generated_at": _now(),
            })
            db.collection("ai_insights").document(article_id).set({
                "article_id":      article_id,
                "source_table":    "articles",
                "title":           article.get("title", ""),
                "topic":           article.get("topic", ""),
                "source":          article.get("source", ""),
                "matched_domains": article.get("matched_domains", []),
                "article_url":     article.get("article_url") or article.get("url", ""),
                "ai_insight":      insight,
                "created_at":      _now(),
            })
            _queue_mark(article_id, "done", "")
            results.append({"article_id": article_id, "title": title,
                            "status": "done", "domain": insight.get("domain", "")})
            succeeded += 1
            log.info("process_queue: done %s (%s)", article_id, title[:50])

        except Exception as e:
            log.error("process_queue: insight failed %s (attempt %d): %s",
                      article_id, attempts, e)
            new_status = "failed" if attempts >= max_attempts else "pending"
            _queue_mark(article_id, new_status, str(e))
            results.append({"article_id": article_id, "title": title,
                            "status": new_status, "error": str(e)[:200],
                            "attempts": attempts})
            failed_count += 1

    try:
        remaining_docs = list(db.collection("insight_queue").where("status", "==", "pending").stream())
        remaining      = len(remaining_docs)
    except Exception:
        remaining = -1

    log.info("process_queue: batch done — succeeded=%d failed=%d remaining=%d",
             succeeded, failed_count, remaining)
    return {
        "processed": len(results), "succeeded": succeeded,
        "failed": failed_count, "remaining": remaining,
        "results": results,
    }


@app.get(
    "/api/articles/insights/queue",
    tags    = ["Insights"],
    summary = "Check insight queue status — ADMIN ONLY",
)
def get_insight_queue_status(payload: dict = Depends(require_admin)):
    """ADMIN ONLY — check the current state of the insight generation queue."""
    try:
        all_docs = db.collection("insight_queue").stream()
        items    = [d.to_dict() for d in all_docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    by_status = {"pending": [], "done": [], "failed": [], "processing": []}
    for item in items:
        s = item.get("status", "pending")
        by_status.setdefault(s, []).append(item)

    return {
        "pending":    len(by_status["pending"]),
        "done":       len(by_status["done"]),
        "failed":     len(by_status["failed"]),
        "processing": len(by_status["processing"]),
        "generating": _insight_thread_running,
        "total":      len(items),
        "items":      sorted(by_status["pending"], key=lambda x: x.get("queued_at", "")),
    }


@app.delete(
    "/api/articles/insights/queue",
    tags    = ["Insights"],
    summary = "Clear insight queue — ADMIN ONLY",
)
def clear_insight_queue(payload: dict = Depends(require_admin)):
    """ADMIN ONLY — delete all items from the insight_queue. Use before a fresh refresh."""
    try:
        docs    = db.collection("insight_queue").stream()
        deleted = 0
        for d in docs:
            db.collection("insight_queue").document(d.id).delete()
            deleted += 1
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("insight_queue cleared: %d items by admin=%s", deleted, get_uid(payload))
    return {"cleared": True, "deleted": deleted}


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — REFRESH CHECK (read-only, any auth)
# ══════════════════════════════════════════════════════════════════════════════

@app.get(
    "/api/articles/refresh/check",
    tags    = ["Articles"],
    summary = "Check for new articles in DB — any auth (read-only)",
)
def refresh_check(
    since_hours: int  = Query(24, ge=1, le=168),
    payload:     dict = Depends(require_auth),
):
    """
    GET /api/articles/refresh/check?since_hours=24
    Any authenticated user — check if new articles have been added recently.
    Does NOT trigger any RSS fetch.
    """
    cutoff     = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    cutoff_iso = cutoff.isoformat()

    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(50).stream()
        )
        new_articles = []
        for d in docs:
            art = d.to_dict()
            if art.get("fetched_at", "") >= cutoff_iso:
                new_articles.append({
                    "id":              art.get("id"),
                    "title":           art.get("title"),
                    "short_summary":   art.get("short_summary", ""),
                    "image_url":       art.get("image_url", ""),
                    "article_url":     art.get("article_url") or art.get("url", ""),
                    "matched_domains": art.get("matched_domains", []),
                    "source":          art.get("source", ""),
                    "fetched_at":      art.get("fetched_at"),
                    "has_insight":     art.get("has_insight", False),
                })
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    return {
        "new_articles": len(new_articles),
        "since_hours":  since_hours,
        "articles":     new_articles,
    }