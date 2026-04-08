"""
STEAMI FastAPI  v6
==================
Run:   uvicorn main:app --host 0.0.0.0 --port 5000 --reload
Docs:  http://127.0.0.1:5000/docs

DUMMY ACCOUNTS (seeded automatically on first startup):
  ADMIN  admin@steami.dev   /  Admin@steami123   (full access)
  MOD    mod@steami.dev     /  Mod@steami123     (content management)
  USER   user@steami.dev    /  User@steami123    (normal browsing + insights)

ROLE PERMISSIONS:
  admin — everything including user management and seeding
  mod   — content management (articles, explainers, research) — no user mgmt
  user  — authenticated features: chat, AI insights, personal feed

PUBLIC (no token):   articles list/get, explainers, research, feed/items, sources
PROTECTED:           insights, chat, article writes, content writes, user management
"""

import os
import uuid
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ── Core modules ───────────────────────────────────────────────────────────
from firestore_client import db
from gemini_client import generate_ai_insight
from article_fetcher import (
    fetch_articles_from_source,
    fetch_articles_from_url,
    get_rss_sources,
)

# ── Auth dependency helpers ────────────────────────────────────────────────
from auth import require_auth, require_mod, require_admin, get_uid

# ── Routers ────────────────────────────────────────────────────────────────
from routers import chat, feed, content
from routers.auth_router import router as auth_router, seed_dummy_accounts

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(levelname)s | %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title       = "STEAMI API",
    version     = "6.0.0",
    description = (
        "STEAMI Backend — articles, AI insights, chat, feed, explainers.\n\n"
        "**Test Accounts (login at POST /api/auth/login):**\n"
        "- Admin: `admin@steami.dev` / `Admin@steami123`\n"
        "- Mod:   `mod@steami.dev`   / `Mod@steami123`\n"
        "- User:  `user@steami.dev`  / `User@steami123`\n\n"
        "Copy the `token` from the login response, click **Authorize** "
        "above, and enter `Bearer <token>` to use protected endpoints."
    ),
    swagger_ui_parameters = {"persistAuthorization": True},
)

# ── CORS ───────────────────────────────────────────────────────────────────
# FastAPI's CORSMiddleware handles OPTIONS preflights automatically —
# no before_request hook needed like we had in Flask.
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = False,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── Startup seed ───────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    """
    Runs once when the server starts.
    Creates the 3 dummy accounts (admin/mod/user) in Firestore if absent.
    """
    log.info("=== STEAMI v6 starting ===")
    result = seed_dummy_accounts()
    log.info("Accounts seeded=%s  skipped=%s", result["created"], result["skipped"])


# ── Routers ────────────────────────────────────────────────────────────────
app.include_router(auth_router,   prefix="/api/auth",  tags=["Auth"])
app.include_router(chat.router,   prefix="/api/chat",  tags=["Chat"])
app.include_router(feed.router,   prefix="/api/feed",  tags=["Feed"])
app.include_router(content.router,prefix="/api",       tags=["Content"])


def _now() -> str:
    """Current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Health"], summary="Health check — public")
def health():
    return {"status": "ok", "ts": _now()}


# ══════════════════════════════════════════════════════════════════════════════
# SOURCES — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sources", tags=["Articles"], summary="List RSS sources — public")
def list_sources():
    """Public: list all RSS sources. No token required."""
    return {"sources": get_rss_sources()}


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST BODIES
# ══════════════════════════════════════════════════════════════════════════════

class FetchArticlesBody(BaseModel):
    topic:    str       = "technology"
    keywords: list[str] = []
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

class PipelineBody(BaseModel):
    topic:    str       = "technology"
    keywords: list[str] = []
    limit:    int       = 3


# ══════════════════════════════════════════════════════════════════════════════
# ARTICLES — FETCH (requires mod/admin)
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/articles/fetch",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch articles from RSS — requires mod/admin",
)
def fetch_and_save(
    body:    FetchArticlesBody,
    payload: dict = Depends(require_mod),  # mod or admin only
):
    """Trigger RSS fetch and save results to Firestore. Requires mod/admin."""
    try:
        raw = fetch_articles_from_source(
            topic=body.topic, keywords=body.keywords, limit=body.limit
        )
    except Exception as e:
        log.error("fetch_and_save: %s", e)
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

    log.info("fetch_and_save: fetched=%d saved=%d by %s", len(raw), len(saved), get_uid(payload))
    return {"saved": len(saved), "articles": saved}


@app.post(
    "/api/articles/fetch-source",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch from URL — requires mod/admin",
)
def fetch_from_source_url(
    body:    FetchSourceBody,
    payload: dict = Depends(require_mod),  # mod or admin only
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
# ARTICLES — CRUD
# GET routes are PUBLIC — anyone can browse
# POST requires mod/admin
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/articles", tags=["Articles"], summary="List articles — PUBLIC")
def list_articles(limit: int = Query(30, ge=1, le=200)):
    """Public: list articles, newest first. No token required."""
    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        return {"articles": [d.to_dict() for d in docs]}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/api/articles/{article_id}", tags=["Articles"], summary="Get article — PUBLIC")
def get_article(article_id: str):
    """Public: get a single article. No token required."""
    doc = db.collection("articles").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Article not found")
    return doc.to_dict()


@app.post(
    "/api/articles",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Create article manually — requires mod/admin",
)
def create_article(
    body:    CreateArticleBody,
    payload: dict = Depends(require_mod),
):
    """Manually create an article. Requires mod/admin."""
    doc_id = str(uuid.uuid4())
    art = {
        "id": doc_id, "title": body.title, "content": body.content,
        "url": body.url, "source": body.source, "topic": body.topic,
        "fetched_at": _now(), "has_insight": False,
    }
    db.collection("articles").document(doc_id).set(art)
    return art


# ══════════════════════════════════════════════════════════════════════════════
# AI INSIGHTS — LOCKED (require_auth = any logged-in user)
# ══════════════════════════════════════════════════════════════════════════════

@app.delete(
    "/api/articles/{article_id}/insight",
    tags    = ["Insights"],
    summary = "Clear cached insight — requires mod/admin",
)
def delete_insight(
    article_id: str,
    payload:    dict = Depends(require_mod),  # mod/admin only — users can't delete insights
):
    """
    Clear the cached AI insight so next POST regenerates from Gemini.
    Requires mod or admin.
    """
    doc_ref = db.collection("articles").document(article_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Article not found")
    try:
        doc_ref.update({"ai_insight": None, "has_insight": False, "insight_generated_at": None})
    except Exception as e:
        log.warning("delete_insight: could not clear fields: %s", e)
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
    payload:    dict = Depends(require_auth),  # ANY logged-in user (user/mod/admin)
):
    """
    **LOCKED — requires login (any role).**

    Generate an AI insight (summary + explanatory SVG) for one article.
    Searches both `articles` and `feed_articles` automatically.
    Results are cached — use ?force=true to regenerate.
    """
    # ── Find article (check both collections) ─────────────────────────────
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
        # Check article-level cache
        cached = article.get("ai_insight")
        if (
            cached and isinstance(cached, dict)
            and cached.get("summary") and not cached.get("raw")
            and len(cached.get("summary", "")) > 50
        ):
            return {"article_id": article_id, "source_table": source_table,
                    "ai_insight": cached, "cached": True}

        # Check shared ai_insights collection
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
                # Old/broken cache — delete so we regenerate cleanly
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

    # ── Persist to article doc ─────────────────────────────────────────────
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

    log.info("generate_insight: OK %s table=%s domain=%s words=%d by user=%s",
             article_id, source_table, insight.get("domain","?"),
             len(insight.get("summary","").split()), get_uid(payload))

    return {"article_id": article_id, "source_table": source_table,
            "ai_insight": insight, "cached": False}


@app.get(
    "/api/insights",
    tags    = ["Insights"],
    summary = "List all insights — REQUIRES LOGIN",
)
def list_insights(
    limit:   int  = Query(50, ge=1, le=200),
    payload: dict = Depends(require_auth),  # any logged-in user
):
    """**LOCKED — requires login.** List all AI insights, newest first."""
    try:
        docs = (
            db.collection("ai_insights")
              .order_by("created_at", direction="DESCENDING")
              .limit(limit).stream()
        )
        return {"insights": [d.to_dict() for d in docs]}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get(
    "/api/insights/{article_id}",
    tags    = ["Insights"],
    summary = "Get single insight — REQUIRES LOGIN",
)
def get_insight(
    article_id: str,
    payload:    dict = Depends(require_auth),  # any logged-in user
):
    """**LOCKED — requires login.** Get a single AI insight by article ID."""
    doc = db.collection("ai_insights").document(article_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Insight not found")
    return doc.to_dict()


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE — requires mod/admin
# ══════════════════════════════════════════════════════════════════════════════

@app.post(
    "/api/pipeline",
    status_code = 201,
    tags        = ["Articles"],
    summary     = "Fetch + generate insights — requires mod/admin",
)
def pipeline(
    body:    PipelineBody,
    payload: dict = Depends(require_mod),
):
    """
    Fetch articles and immediately generate insights for all of them.
    Heavy operation — use sparingly. Requires mod/admin.
    """
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