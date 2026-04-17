"""
STEAMI FastAPI  v7 — MongoDB Atlas backend
==================
Run:   uvicorn main:app --host 0.0.0.0 --port 5000 --reload
Docs:  http://127.0.0.1:5000/docs

NEW IN v7:
  - Signup: profession field instead of domain/background/statement
  - POST /api/auth/interests   — save user STEM topic interests after signup
  - GET  /api/auth/interests   — get own interests
  - POST /api/articles/refresh — expire articles >25 days (was 30), also deletes
                                  their ai_insights; fetch min 3 per topic
  - GET  /api/articles/for-me  — articles filtered by user's saved interests
  - POST /api/diary            — save selected content to personal diary
  - GET  /api/diary            — list own diary entries
  - POST /api/dashboard/event  — log popup open event (for analytics)
  - GET  /api/dashboard/me     — own activity summary
  - GET  /api/dashboard/admin  — platform-wide stats (admin only)

IMAGE SUPPORT (v7.1):
  - Images folder served as static files at /images/...
  - POST /api/images/upload — upload new image (mod/admin)
  - POST /api/explainers/seed and /api/research/seed now store image URLs
  - All GET /api/explainers and /api/research/articles responses include "image"

DUMMY ACCOUNTS (auto-created on startup):
  admin@steami.dev / Admin@steami123  (admin)
  mod@steami.dev   / Mod@steami123    (mod)
  user@steami.dev  / User@steami123   (user)

ROUTE PROTECTION:
  Public:       GET articles, GET explainers, GET research, GET feed/items
  Any auth:     insights, chat, diary, dashboard events, for-me feed
  mod/admin:    article fetch, article write, explainer/research writes, image upload
  admin only:   user mgmt, seed, delete explainers/research
"""

import os
import uuid
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles          # ← NEW
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Core modules ───────────────────────────────────────────────────────────
from mongodb_client import db
from ollama_agent import generate_ai_insight   # Gemma 4 via Ollama Cloud
from article_fetcher import (
    fetch_articles_from_source,
    fetch_articles_from_url,
    fetch_articles_by_domains,
    get_rss_sources,
    DOMAIN_KEYWORDS,
    ALL_DOMAINS,
)

# ── Auth dependency helpers ────────────────────────────────────────────────
from auth import require_auth, require_mod, require_admin, get_uid

# ── Routers ────────────────────────────────────────────────────────────────
from routers import chat, feed, content
from routers.auth_router  import router as auth_router, seed_dummy_accounts
from routers.diary        import router as diary_router
from routers.dashboard    import router as dashboard_router

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)s | %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger(__name__)

# Article expiry period — articles older than this are deleted on refresh
EXPIRY_DAYS = 25   # changed from 30 to 25

# Root folder for uploaded / seeded images (must exist at server root)
IMAGES_DIR = "images"


# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title       = "STEAMI API",
    version     = "8.0.0",
    description = (
        "STEAMI Backend — articles, insights, chat, feed, explainers, diary, dashboard.\n\n"
        "**Test Accounts (POST /api/auth/login):**\n"
        "- Admin: `admin@steami.dev` / `Admin@steami123`\n"
        "- Mod:   `mod@steami.dev`   / `Mod@steami123`\n"
        "- User:  `user@steami.dev`  / `User@steami123`\n\n"
        "Paste the `token` value into **Authorize → Bearer <token>** above.\n\n"
        "**Images** are served from `/images/research/` and `/images/explainers/`.\n"
        "Upload new images via `POST /api/images/upload` (mod/admin)."
    ),
    swagger_ui_parameters = {"persistAuthorization": True},
)

# ── CORS — allow all origins, handles OPTIONS automatically ────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = False,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Static file serving for images ────────────────────────────────────────
# Creates the images directory if it doesn't exist, then mounts it.
# After this, http://host:5000/images/research/physics.jpg works directly.
os.makedirs(os.path.join(IMAGES_DIR, "research"),   exist_ok=True)
os.makedirs(os.path.join(IMAGES_DIR, "explainers"), exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")  # ← NEW


# ── Startup ────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    """
    Runs once on server start.
    Seeds the 3 dummy accounts into Firestore if they don't already exist.
    """
    log.info("=== STEAMI v8.0 starting ===")
    result = seed_dummy_accounts()
    log.info("Accounts seeded=%s skipped=%s", result["created"], result["skipped"])


# ── Routers ────────────────────────────────────────────────────────────────
app.include_router(auth_router,      prefix="/api/auth",      tags=["Auth"])  # includes /newsletter/recipients
app.include_router(chat.router,      prefix="/api/chat",      tags=["Chat"])
app.include_router(feed.router,      prefix="/api/feed",      tags=["Feed"])
app.include_router(content.router,   prefix="/api",           tags=["Content"])
app.include_router(diary_router,     prefix="/api/diary",     tags=["Diary"])
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"])


def _now() -> str:
    """Current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp string to a timezone-aware datetime. Returns None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"], summary="Health check — public")
def health():
    return {"status": "ok", "version": "8.0.0", "ts": _now()}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCES — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sources", tags=["Articles"], summary="List RSS sources — public")
def list_sources():
    """Public: list all RSS sources used for article fetching."""
    return {"sources": get_rss_sources()}


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST BODIES
# ══════════════════════════════════════════════════════════════════════════════

class FetchArticlesBody(BaseModel):
    """Body for POST /api/articles/fetch"""
    topic:    str       = "technology"
    keywords: list[str] = []
    limit:    int       = 20


class FetchSourceBody(BaseModel):
    """Body for POST /api/articles/fetch-source"""
    url:   str
    limit: int = 20


class CreateArticleBody(BaseModel):
    """Body for POST /api/articles"""
    title:   str
    content: str
    url:     str = ""
    source:  str = "manual"
    topic:   str = "general"


class RefreshBody(BaseModel):
    """Body for POST /api/articles/refresh"""
    domains: list[str] = []   # subset of ALL_DOMAINS — empty means all 10
    target:  int        = 30  # target number of articles to fetch


class PipelineBody(BaseModel):
    """Body for POST /api/pipeline"""
    topic:    str       = "technology"
    keywords: list[str] = []
    limit:    int       = 3

# NOTE: The remainder of main.py (all the article/insight/pipeline routes)
# is unchanged from v7. Paste your existing route handlers below this line.
# Only the top section above needed updating for image support.
# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — REFRESH  (expire old, fetch new by domain topics)
# Requires: mod | admin
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/articles/refresh",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Refresh articles: expire old (>25d) + fetch new by topics — requires mod/admin",
)
def refresh_articles(
    body:    RefreshBody = RefreshBody(),
    payload: dict        = Depends(require_mod),   # mod or admin only
):
    """
    POST /api/articles/refresh

    Does three things in order:
    1. Loads all articles from Firestore and identifies those older than 25 days.
    2. Deletes expired articles AND their corresponding ai_insights documents.
    3. Fetches fresh articles from the 3 primary RSS sources, filtered by the
       10 canonical STEM topics. Guarantees at least 3 articles per topic.
       Only saves articles whose URL is not already in the database.

    Body (optional):
    {
      "domains": ["AI + ROBOTICS", "PHYSICS"],  // omit for all 10 topics
      "target":  30                              // desired total articles
    }

    Response:
    {
      "deleted_articles": 8,    // articles older than 25 days that were removed
      "deleted_insights":  8,    // their ai_insights also removed
      "fetched":          28,    // articles pulled from RSS
      "new_saved":        22,    // articles actually saved (not already present)
      "skipped":           6,    // already existed in Firestore
      "articles":        [ ...new saved articles... ]
    }

    curl -X POST http://127.0.0.1:5000/api/articles/refresh \\
      -H "Authorization: Bearer <mod_or_admin_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"domains":["AI + ROBOTICS","PHYSICS"],"target":30}'
    """
    # Validate domains — empty list means use all 10
    active_domains = [d for d in body.domains if d in DOMAIN_KEYWORDS] or ALL_DOMAINS
    cutoff         = datetime.now(timezone.utc) - timedelta(days=EXPIRY_DAYS)

    # ── Step 1: Load all existing articles to find expired ones ───────────
    try:
        all_docs = db.collection("articles").stream_all()
    except Exception as e:
        log.error("refresh: failed to load articles: %s", e)
        raise HTTPException(500, detail=f"Firestore read failed: {e}")

    existing_urls: set[str]  = set()
    expired_ids:   list[str] = []

    for doc in all_docs:
        d = doc.to_dict()

        # Track URLs so we can skip duplicates when saving new articles
        url = d.get("article_url") or d.get("url", "")
        if url:
            existing_urls.add(url)

        # Mark expired: older than EXPIRY_DAYS (25 days)
        fetched_at = _parse_dt(d.get("fetched_at"))
        if fetched_at and fetched_at < cutoff:
            expired_ids.append(doc.id)

    log.info(
        "refresh: total=%d existing, %d expired (>%dd), cutoff=%s",
        len(all_docs), len(expired_ids), EXPIRY_DAYS, cutoff.date(),
    )

    # ── Step 2: Deletion disabled (removed by design) ────────────────────
    # Articles and insights are NEVER deleted automatically.
    # expired_ids are identified above but intentionally NOT deleted.
    # The fetcher will skip duplicate URLs, so old articles stay visible
    # in the database but new articles with the same URL won't be duplicated.
    deleted_articles = 0
    deleted_insights = 0
    log.info("refresh: found %d articles older than %dd (deletion disabled)",
             len(expired_ids), EXPIRY_DAYS)

    # ── Step 3: Fetch fresh articles (min 3 per topic) ────────────────────
    try:
        raw = fetch_articles_by_domains(
            active_domains = active_domains,
            target_total   = body.target,
        )
    except Exception as e:
        log.error("refresh: fetch failed: %s", e)
        raise HTTPException(502, detail=f"RSS fetch failed: {e}")

    # ── Step 4: Save only NEW articles (skip URLs already in database) ──────
    saved:   list[dict] = []
    skipped: int        = 0

    for art in raw:
        art_url = art.get("article_url") or art.get("url", "")

        # Skip if this URL is already in the database
        if art_url and art_url in existing_urls:
            skipped += 1
            continue

        # Prepare the article document
        art.setdefault("id", str(uuid.uuid4()))
        art["fetched_at"]  = _now()
        art["has_insight"] = False

        try:
            db.collection("articles").document(art["id"]).set(art)
            saved.append(art)
            if art_url:
                existing_urls.add(art_url)
        except Exception as e:
            log.error("refresh: MongoDB save failed for %s: %s", art["id"], e)

    # ── Step 5: Add all newly saved articles to the insight queue ───────────
    # Instead of generating insights inline (which causes 120s timeouts for
    # 30 articles), we queue them. The admin calls POST /api/articles/insights/process
    # every 5 minutes to generate 2 insights per batch — no timeouts.
    queued = 0
    for art in saved:
        try:
            # Only queue if no insight exists yet
            existing = db.collection("ai_insights").document(art["id"]).get()
            if existing.exists:
                continue  # already has an insight — skip

            db.collection("insight_queue").document(art["id"]).set({
                "article_id":    art["id"],
                "title":         art.get("title", ""),
                "matched_domains": art.get("matched_domains", []),
                "queued_at":     _now(),
                "status":        "pending",    # pending → processing → done / failed
                "attempts":      0,
                "last_error":    "",
            })
            queued += 1
        except Exception as e:
            log.error("refresh: failed to queue article %s: %s", art.get("id"), e)

    # Count total pending items in queue
    try:
        all_pending = db.collection("insight_queue").where("status", "==", "pending").stream()
        queue_total = len(all_pending)
    except Exception:
        queue_total = queued

    log.info(
        "refresh done: fetched=%d new_saved=%d skipped=%d queued=%d queue_total=%d",
        len(raw), len(saved), skipped, queued, queue_total,
    )

    return {
        "expired_found": len(expired_ids),
        "fetched":       len(raw),
        "new_saved":     len(saved),
        "skipped":       skipped,
        "queued":        queued,
        "queue_total":   queue_total,
        "articles":      saved,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — FILTERED BY USER INTERESTS
# Requires: any auth (user | mod | admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.get(
    "/api/articles/for-me",
    tags    = ["Articles"],
    summary = "Articles filtered by your saved interests — requires auth",
)
def articles_for_me(
    limit:   int  = Query(30, ge=1, le=200),
    payload: dict = Depends(require_auth),   # any logged-in user
):
    """
    GET /api/articles/for-me?limit=30
    Returns articles that match the current user's saved topic interests.

    The user's interests are loaded from Firestore (set via POST /api/auth/interests).
    Articles are filtered where their matched_domains overlap with the user's interests.
    Ensures at least one article per interest topic if available.

    If the user has no interests saved, returns all recent articles.

    Response:
    {
      "uid":       "user-uuid",
      "interests": ["AI + ROBOTICS", "PHYSICS"],
      "total":     18,
      "articles":  [ { id, title, short_summary, image_url, matched_domains, ... }, ... ]
    }

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/articles/for-me
    """
    uid = get_uid(payload)

    # ── Load user's saved interests ────────────────────────────────────────
    user_interests: list[str] = []
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            user_interests = user_doc.to_dict().get("interests", [])
    except Exception as e:
        log.warning("articles_for_me: could not load user %s: %s", uid, e)

    # ── Load recent articles from Firestore ───────────────────────────────
    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(300)   # load a big pool to filter from
              .stream()
        )
        all_articles = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    # ── If no interests set, return recent articles ────────────────────────
    if not user_interests:
        return {
            "uid":       uid,
            "interests": [],
            "total":     len(all_articles[:limit]),
            "articles":  all_articles[:limit],
        }

    interests_set = set(user_interests)

    # ── Filter articles whose matched_domains overlap with interests ───────
    def _article_matches(art: dict) -> bool:
        """Check if this article is relevant to the user's interests."""
        matched = set(art.get("matched_domains") or [])
        if matched & interests_set:
            return True
        # Fallback: scan title + content for interest keywords
        text = (art.get("title", "") + " " + art.get("content", "")).lower()
        for topic in user_interests:
            kws = DOMAIN_KEYWORDS.get(topic, [])
            if any(kw.lower() in text for kw in kws):
                return True
        return False

    candidate_articles = [a for a in all_articles if _article_matches(a)]

    # ── Guarantee at least 1 article per interest topic ───────────────────
    topic_covered: set[str]  = set()
    selected_ids:  set[str]  = set()
    result:        list[dict] = []

    # Pass 1: pick one article per interest topic
    for topic in user_interests:
        if topic in topic_covered:
            continue
        for art in candidate_articles:
            if art["id"] in selected_ids:
                continue
            # Check if this article covers the topic
            matched = set(art.get("matched_domains") or [])
            text    = (art.get("title", "") + " " + art.get("content", "")).lower()
            topic_kws = [k.lower() for k in DOMAIN_KEYWORDS.get(topic, [])]
            if topic in matched or any(k in text for k in topic_kws):
                result.append(art)
                selected_ids.add(art["id"])
                topic_covered.add(topic)
                break

    # Pass 2: fill remaining slots up to limit
    for art in candidate_articles:
        if len(result) >= limit:
            break
        if art["id"] not in selected_ids:
            result.append(art)
            selected_ids.add(art["id"])

    # Strip heavy fields not needed in the list view
    slim_fields = [
        "id", "title", "short_summary", "image_url", "article_url",
        "url", "matched_domains", "source", "published_at",
        "fetched_at", "has_insight", "topic",
    ]
    slim = [{k: a.get(k) for k in slim_fields} for a in result[:limit]]

    log.info(
        "articles_for_me: uid=%s interests=%s candidate=%d returned=%d",
        uid, user_interests, len(candidate_articles), len(slim),
    )

    return {
        "uid":       uid,
        "interests": user_interests,
        "total":     len(slim),
        "articles":  slim,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — FETCH FROM RSS  (requires mod/admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/articles/fetch",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch articles from RSS by topic — requires mod/admin",
)
def fetch_and_save(
    body:    FetchArticlesBody,
    payload: dict = Depends(require_mod),
):
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
            log.error("Firestore save failed for %s: %s", art["id"], e)

    return {"saved": len(saved), "articles": saved}


@app.post(
    "/api/articles/fetch-source",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch from a URL — requires mod/admin",
)
def fetch_from_source_url(
    body:    FetchSourceBody,
    payload: dict = Depends(require_mod),
):
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
            log.error("Firestore save failed: %s", e)

    return {"saved": len(saved), "articles": saved, "source_url": url}


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — CRUD  (GET routes are PUBLIC)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/articles", tags=["Articles"], summary="List articles — PUBLIC")
def list_articles(limit: int = Query(30, ge=1, le=200)):
    """Public: list all articles, newest first. No token required."""
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
# AI INSIGHTS — LOCKED (require_auth for generate/read, require_mod to delete)
# ══════════════════════════════════════════════════════════════════════════════

@app.delete(
    "/api/articles/{article_id}/insight",
    tags    = ["Insights"],
    summary = "Clear cached insight — requires mod/admin",
)
def delete_insight(
    article_id: str,
    payload:    dict = Depends(require_mod),
):
    """
    Clear the cached AI insight so next POST regenerates from Gemini.
    Also deletes the entry from the ai_insights collection.
    Requires mod or admin.
    """
    doc_ref = db.collection("articles").document(article_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Article not found")

    # Clear insight fields on the article document
    try:
        doc_ref.update({
            "ai_insight":           None,
            "has_insight":          False,
            "insight_generated_at": None,
        })
    except Exception as e:
        log.warning("delete_insight: could not clear fields: %s", e)

    # Delete from the dedicated ai_insights collection
    try:
        db.collection("ai_insights").document(article_id).delete()
    except Exception as e:
        log.warning("delete_insight: could not delete ai_insights doc: %s", e)

    log.info("delete_insight: cleared %s by %s", article_id, get_uid(payload))
    return {"deleted": True, "article_id": article_id}


@app.post(
    "/api/articles/{article_id}/insight",
    tags    = ["Insights"],
    summary = "Generate AI insight — REQUIRES LOGIN",
)
def generate_insight(
    article_id: str,
    force:      bool = Query(False, description="true = skip cache and regenerate"),
    payload:    dict = Depends(require_auth),   # any logged-in user
):
    """
    **LOCKED — requires any valid login (user/mod/admin).**
    Generate an AI insight (summary + SVG diagram) for one article on demand.
    Searches both `articles` and `feed_articles` automatically.
    """
    # ── Find article in either collection ─────────────────────────────────
    source_table = "articles"
    doc_ref      = db.collection("articles").document(article_id)
    doc          = doc_ref.get()
    if not doc.exists:
        doc_ref      = db.collection("feed_articles").document(article_id)
        doc          = doc_ref.get()
        source_table = "feed_articles"
    if not doc.exists:
        raise HTTPException(404, detail="Article not found in articles or feed_articles")

    article = doc.to_dict()

    # ── Cache check ────────────────────────────────────────────────────────
    if not force:
        cached = article.get("ai_insight")
        if (
            cached and isinstance(cached, dict)
            and cached.get("summary") and not cached.get("raw")
            and len(cached.get("summary", "")) > 50
        ):
            return {"article_id": article_id, "source_table": source_table,
                    "ai_insight": cached, "cached": True}

        insight_doc = db.collection("ai_insights").document(article_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (isinstance(stored, dict) and stored.get("summary")
                    and not stored.get("raw") and len(stored.get("summary", "")) > 50):
                return {
                    "article_id":   article_id,
                    "source_table": insight_doc.to_dict().get("source_table", source_table),
                    "ai_insight":   stored,
                    "cached":       True,
                }
            else:
                # Old/broken cache — delete so we can regenerate
                try:
                    db.collection("ai_insights").document(article_id).delete()
                except Exception:
                    pass

    # ── Generate via Gemini ────────────────────────────────────────────────
    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        log.error("generate_insight: Gemini error for %s: %s", article_id, e)
        raise HTTPException(502, detail=str(e))

    # ── Persist to article document ────────────────────────────────────────
    try:
        doc_ref.update({
            "ai_insight": insight, "has_insight": True, "insight_generated_at": _now()
        })
    except Exception as e:
        log.error("generate_insight: article update failed: %s", e)

    # ── Persist to ai_insights collection ─────────────────────────────────
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
        log.error("generate_insight: ai_insights save failed: %s", e)

    log.info("generate_insight: OK %s table=%s domain=%s words=%d by=%s",
             article_id, source_table, insight.get("domain","?"),
             len(insight.get("summary","").split()), get_uid(payload))

    return {"article_id": article_id, "source_table": source_table,
            "ai_insight": insight, "cached": False}


@app.get("/api/insights", tags=["Insights"], summary="List all insights — REQUIRES LOGIN")
def list_insights(
    limit:   int  = Query(50, ge=1, le=200),
    payload: dict = Depends(require_auth),
):
    """**LOCKED.** List all AI insights, newest first."""
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
         summary="Get single insight — REQUIRES LOGIN")
def get_insight(article_id: str, payload: dict = Depends(require_auth)):
    """**LOCKED.** Get a single AI insight by article ID."""
    doc = db.collection("ai_insights").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Insight not found")
    return doc.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE  (legacy — requires mod/admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/pipeline", status_code=201, tags=["Articles"],
          summary="Fetch + generate insights — requires mod/admin")
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
                "ai_insight": insight, "has_insight": True, "insight_generated_at": _now()
            })
            db.collection("ai_insights").document(art["id"]).set({
                "article_id": art["id"], "title": art.get("title", ""),
                "topic": body.topic, "ai_insight": insight, "created_at": _now()
            })
            results.append({"id": art["id"], "title": art.get("title",""),
                            "ai_insight": insight, "status": "ok"})
        except Exception as e:
            results.append({"id": art["id"], "status": "error", "error": str(e)})

    return {"processed": len(results), "results": results}

# ══════════════════════════════════════════════════════════════════════════════
# INSIGHT QUEUE — batch processor and status endpoints
# ══════════════════════════════════════════════════════════════════════════════

# How the queue works:
#   1. POST /api/articles/refresh  (admin) → saves articles + adds each to insight_queue
#   2. POST /api/articles/insights/process (admin) → picks next batch_size pending items,
#      generates insights one by one, marks each done/failed
#   3. Admin calls step 2 every 5 minutes (cron job or manual clicks in admin panel)
#
# With batch_size=2 and 30 articles:
#   15 batches × 5 min interval = 75 minutes total — no timeouts
#
# MongoDB collection: insight_queue
# Document fields:
#   article_id, title, matched_domains, queued_at, status, attempts, last_error
#
# Status values:
#   pending    — waiting to be processed
#   processing — currently being processed (set at start, in case of crash)
#   done       — insight generated and saved
#   failed     — gave up after max_attempts


class ProcessBody(BaseModel):
    """
    Body for POST /api/articles/insights/process
    batch_size: how many insights to generate in this call (default 2)
    """
    batch_size: int = 2   # generate this many insights per call


@app.post(
    "/api/articles/insights/process",
    status_code = 200,
    tags        = ["Insights"],
    summary     = "Process next batch from insight queue — ADMIN ONLY",
)
def process_insight_queue(
    body:    ProcessBody = ProcessBody(),
    payload: dict        = Depends(require_admin),   # ADMIN ONLY
):
    """
    POST /api/articles/insights/process
    ADMIN ONLY — processes the next N articles from the insight_queue.

    Call this endpoint every 5 minutes to gradually generate insights
    without hitting the Ollama 120-second timeout:
      - batch_size=2 (default) → 2 insights per call
      - 30 articles → 15 calls × 5 min = 75 minutes total

    The endpoint:
    1. Picks the oldest `batch_size` pending queue items
    2. For each: generates the AI insight via Ollama Cloud
    3. Saves insight to the article doc + ai_insights collection
    4. Marks queue item as "done" (or "failed" on error)
    5. Returns what was processed + how many items remain

    Body (optional):
    { "batch_size": 2 }   // how many to process this call

    Response:
    {
      "processed":    2,
      "succeeded":    2,
      "failed":       0,
      "remaining":    18,   // still pending in queue
      "results": [
        { "article_id": "...", "title": "...", "status": "done" },
        { "article_id": "...", "title": "...", "status": "done" }
      ]
    }

    curl -X POST http://127.0.0.1:5000/api/articles/insights/process \\
      -H "Authorization: Bearer <admin_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"batch_size": 2}'
    """
    batch_size   = max(1, min(body.batch_size, 10))  # cap at 10 per call
    max_attempts = 3  # give up after this many failed attempts per article
    results      = []
    succeeded    = 0
    failed       = 0

    # ── Pick the next batch_size pending items, oldest first ──────────────
    try:
        pending_docs = (
            db.collection("insight_queue")
              .where("status", "==", "pending")
              .order_by("queued_at", direction="ASCENDING")
              .limit(batch_size)
              .stream()
        )
        pending = [d.to_dict() for d in pending_docs]
    except Exception as e:
        raise HTTPException(500, detail=f"Could not read insight_queue: {e}")

    if not pending:
        # Count how many failed/done items exist for context
        try:
            done_docs   = db.collection("insight_queue").where("status", "==", "done").stream()
            failed_docs = db.collection("insight_queue").where("status", "==", "failed").stream()
            done_count   = len(done_docs)
            failed_count = len(failed_docs)
        except Exception:
            done_count = failed_count = 0

        return {
            "processed":  0,
            "succeeded":  0,
            "failed":     0,
            "remaining":  0,
            "done_total": done_count,
            "failed_total": failed_count,
            "message":    "Queue is empty — all articles have been processed.",
            "results":    [],
        }

    # ── Process each item ──────────────────────────────────────────────────
    for item in pending:
        article_id = item["article_id"]
        title      = item.get("title", "")
        attempts   = item.get("attempts", 0) + 1

        # Mark as processing so we don't double-pick if something goes wrong
        try:
            db.collection("insight_queue").document(article_id).update({
                "status":   "processing",
                "attempts": attempts,
            })
        except Exception as e:
            log.warning("process_queue: could not mark processing for %s: %s", article_id, e)

        # Fetch the full article from MongoDB
        try:
            art_doc = db.collection("articles").document(article_id).get()
            if not art_doc.exists:
                raise ValueError(f"Article {article_id} not found in articles collection")
            article = art_doc.to_dict()
        except Exception as e:
            log.error("process_queue: could not load article %s: %s", article_id, e)
            try:
                db.collection("insight_queue").document(article_id).update({
                    "status":     "failed" if attempts >= max_attempts else "pending",
                    "last_error": str(e),
                })
            except Exception:
                pass
            results.append({
                "article_id": article_id,
                "title":      title,
                "status":     "failed",
                "error":      str(e),
            })
            failed += 1
            continue

        # Generate the AI insight
        try:
            log.info("process_queue: generating insight for %s (attempt %d)", article_id, attempts)
            insight = generate_ai_insight(article)

            # Save insight to the article document
            db.collection("articles").document(article_id).update({
                "ai_insight":           insight,
                "has_insight":          True,
                "insight_generated_at": _now(),
            })

            # Save to the shared ai_insights collection
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

            # Mark queue item as done
            db.collection("insight_queue").document(article_id).update({
                "status":       "done",
                "completed_at": _now(),
                "last_error":   "",
            })

            results.append({
                "article_id": article_id,
                "title":      title,
                "status":     "done",
                "domain":     insight.get("domain", ""),
            })
            succeeded += 1
            log.info("process_queue: done %s (%s)", article_id, title[:50])

        except Exception as e:
            log.error("process_queue: insight failed for %s (attempt %d): %s",
                      article_id, attempts, e)

            # If too many attempts, mark as permanently failed
            new_status = "failed" if attempts >= max_attempts else "pending"
            try:
                db.collection("insight_queue").document(article_id).update({
                    "status":     new_status,
                    "last_error": str(e)[:500],
                })
            except Exception:
                pass

            results.append({
                "article_id": article_id,
                "title":      title,
                "status":     new_status,
                "error":      str(e)[:200],
                "attempts":   attempts,
            })
            failed += 1

    # Count remaining pending items after this batch
    try:
        remaining_docs = db.collection("insight_queue").where("status", "==", "pending").stream()
        remaining      = len(remaining_docs)
    except Exception:
        remaining = -1   # unknown, don't fail the response

    log.info("process_queue: batch done — succeeded=%d failed=%d remaining=%d",
             succeeded, failed, remaining)
    return {
        "processed":  len(results),
        "succeeded":  succeeded,
        "failed":     failed,
        "remaining":  remaining,
        "results":    results,
    }


@app.get(
    "/api/articles/insights/queue",
    tags    = ["Insights"],
    summary = "Check insight queue status — ADMIN ONLY",
)
def get_insight_queue_status(payload: dict = Depends(require_admin)):
    """
    GET /api/articles/insights/queue
    ADMIN ONLY — check the current state of the insight generation queue.

    Use this to monitor progress while insights are being generated.

    Response:
    {
      "pending":    18,   // waiting to be processed
      "done":       4,    // successfully completed
      "failed":     1,    // gave up after 3 attempts
      "processing": 0,    // currently mid-generation (should be 0 when idle)
      "total":      23,
      "items": [           // all pending items (so admin knows what's coming)
        { "article_id": "...", "title": "...", "queued_at": "...", "attempts": 0 },
        ...
      ]
    }

    curl -H "Authorization: Bearer <admin_token>" \\
      http://127.0.0.1:5000/api/articles/insights/queue
    """
    try:
        all_docs = db.collection("insight_queue").stream()
        items    = [d.to_dict() for d in all_docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    # Group by status
    by_status = {"pending": [], "done": [], "failed": [], "processing": []}
    for item in items:
        s = item.get("status", "pending")
        if s in by_status:
            by_status[s].append(item)
        else:
            by_status["pending"].append(item)

    return {
        "pending":    len(by_status["pending"]),
        "done":       len(by_status["done"]),
        "failed":     len(by_status["failed"]),
        "processing": len(by_status["processing"]),
        "total":      len(items),
        "items":      sorted(by_status["pending"], key=lambda x: x.get("queued_at", "")),
    }


@app.delete(
    "/api/articles/insights/queue",
    tags    = ["Insights"],
    summary = "Clear insight queue — ADMIN ONLY",
)
def clear_insight_queue(payload: dict = Depends(require_admin)):
    """
    DELETE /api/articles/insights/queue
    ADMIN ONLY — delete all items from the insight_queue collection.
    Use this to reset after errors or before a fresh refresh.

    curl -X DELETE http://127.0.0.1:5000/api/articles/insights/queue \\
      -H "Authorization: Bearer <admin_token>"
    """
    try:
        docs    = db.collection("insight_queue").stream()
        deleted = 0
        for d in docs:
            db.collection("insight_queue").document(d.id).delete()
            deleted += 1
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("insight_queue cleared: %d items deleted by admin=%s", deleted, get_uid(payload))
    return {"cleared": True, "deleted": deleted}


@app.get(
    "/api/articles/refresh/check",
    tags    = ["Articles"],
    summary = "Check for new articles in DB — any auth (read-only)",
)
def refresh_check(
    since_hours: int  = Query(24, ge=1, le=168, description="Look for articles added in the last N hours"),
    payload:     dict = Depends(require_auth),   # any logged-in user
):
    """
    GET /api/articles/refresh/check?since_hours=24
    ANY authenticated user — check if new articles have been added to the
    database recently, without triggering any RSS fetch.

    This is the user-facing "refresh" — it just reads the DB.
    Only admins can trigger actual RSS fetching via POST /api/articles/refresh.

    Response:
    {
      "new_articles":  5,    // articles added in the last since_hours hours
      "since_hours":   24,
      "articles": [ ...the new articles, newest first... ]
    }

    curl -H "Authorization: Bearer <token>" \\
      "http://127.0.0.1:5000/api/articles/refresh/check?since_hours=24"
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    cutoff_iso = cutoff.isoformat()

    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(50)
              .stream()
        )
        new_articles = []
        for d in docs:
            art = d.to_dict()
            fetched_str = art.get("fetched_at", "")
            if not fetched_str:
                continue
            # Keep only articles newer than the cutoff
            if fetched_str >= cutoff_iso:
                # Return slim fields — no heavy content/full_content
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