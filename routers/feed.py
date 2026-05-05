"""
routers/feed.py  —  Feed API  v9
==================================
Changes from v8:
  - MAX_ARTICLES reduced from 7 → 4 (per request).
  - AI insights for feed articles are now generated automatically in a
    background thread (2–3 min gap between each), same pattern as main articles.
    The endpoint returns immediately — users see insights appear in real-time.
  - POST /api/feed/items/{id}/insight  now requires mod/admin (was any auth).
    Regular users can no longer trigger insight generation manually.
  - from-selection endpoint remains PUBLIC.

Flow when user selects text and clicks Feed:

  1. Split selected_text into paragraphs (double-newline separated).
  2. Search `feed_articles` DB for articles matching the paragraphs.
  3. If ≥ MIN_ARTICLES (2) DB matches found → return those (no RSS needed).
  4. Otherwise fall back to RSS: score by keywords, pick 2-4 articles,
     enrich (image/summary/full_content), save to `feed_articles`.
  5. Return articles immediately (insights may still be generating).
  6. Background thread generates AI insight for each article with 2-3 min gaps.

ENDPOINTS:
  POST   /api/feed/from-selection         — main pipeline (public)
  GET    /api/feed/items                  — list feed articles (public)
  GET    /api/feed/items/{id}             — single feed article (public)
  DELETE /api/feed/items/{id}             — delete (requires auth)
  POST   /api/feed/items/{id}/insight     — get/generate insight (mod/admin only)
  DELETE /api/feed/cleanup                — delete feed articles + insights older than
                                            N days (default 15); mod/admin only
"""

import uuid
import re
import time
import random
import threading
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from mongodb_client import db
from ollama_agent import generate_ai_insight
from auth import require_auth, require_mod, get_uid
from article_fetcher import (
    RSS_SOURCES,
    DOMAIN_KEYWORDS,
    _fetch_rss_raw,
    _enrich_article,
    _deduplicate,
)

log    = logging.getLogger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MIN_ARTICLES = 2   # minimum articles to return
MAX_ARTICLES = 4   # maximum articles to return (reduced from 7)

LONG_SENTENCE_MIN_WORDS = 8

# Delay between insight generations (seconds)
FEED_INSIGHT_DELAY_MIN = 120   # 2 minutes
FEED_INSIGHT_DELAY_MAX = 180   # 3 minutes

_STOP_WORDS = {
    "the","a","an","and","or","but","in","on","at","to","for","of","with",
    "is","was","are","were","be","been","being","have","has","had","do","does",
    "did","will","would","could","should","may","might","shall","can",
    "this","that","these","those","it","its","i","we","you","he","she","they",
    "not","no","so","if","as","by","from","up","out","about","into","than",
    "then","there","when","where","who","which","what","how","all","any",
    "both","each","few","more","most","other","some","such","very","just",
}


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND INSIGHT GENERATOR (feed-specific)
# ─────────────────────────────────────────────────────────────────────────────

# Per-router thread guard
_feed_insight_thread_lock    = threading.Lock()
_feed_insight_thread_running = False


def _generate_feed_insights_background(article_ids: list[str]) -> None:
    """
    Background thread: generate AI insights for feed articles with 2-3 min gaps.
    Saves each insight immediately so the frontend can show real-time progress.
    """
    global _feed_insight_thread_running

    log.info("feed_insight_bg: starting for %d feed articles", len(article_ids))

    for i, article_id in enumerate(article_ids):
        delay = random.randint(FEED_INSIGHT_DELAY_MIN, FEED_INSIGHT_DELAY_MAX) if i > 0 else 5
        log.info("feed_insight_bg: waiting %ds before article %d/%d", delay, i + 1, len(article_ids))
        time.sleep(delay)

        # Load article
        try:
            doc = db.collection("feed_articles").document(article_id).get()
            if not doc.exists:
                log.warning("feed_insight_bg: %s not found in feed_articles", article_id)
                continue
            article = doc.to_dict()
        except Exception as e:
            log.error("feed_insight_bg: load error %s: %s", article_id, e)
            continue

        # Skip if already has insight
        if article.get("has_insight"):
            log.info("feed_insight_bg: skip %s (already has insight)", article_id)
            continue

        # Generate
        try:
            insight = generate_ai_insight(article)
        except Exception as e:
            log.error("feed_insight_bg: generate failed %s: %s", article_id, e)
            continue

        # Save to feed_articles
        try:
            db.collection("feed_articles").document(article_id).update({
                "ai_insight": insight, "has_insight": True,
                "insight_generated_at": _now(),
            })
        except Exception as e:
            log.error("feed_insight_bg: feed_articles update failed %s: %s", article_id, e)

        # Save to shared ai_insights (real-time visible)
        try:
            db.collection("ai_insights").document(article_id).set({
                "article_id":      article_id,
                "source_table":    "feed_articles",
                "title":           article.get("title", ""),
                "topic":           (article.get("matched_domains") or ["Technology"])[0],
                "source":          article.get("source", ""),
                "matched_domains": article.get("matched_domains", []),
                "article_url":     article.get("article_url") or article.get("url", ""),
                "keywords":        article.get("keywords", []),
                "selected_text":   article.get("selected_text", ""),
                "ai_insight":      insight,
                "created_at":      _now(),
            })
        except Exception as e:
            log.error("feed_insight_bg: ai_insights save failed %s: %s", article_id, e)

        log.info("feed_insight_bg: done %s domain=%s", article_id, insight.get("domain", "?"))

    with _feed_insight_thread_lock:
        global _feed_insight_thread_running   # noqa
        _feed_insight_thread_running = False

    log.info("feed_insight_bg: thread finished")


def _start_feed_insight_thread(article_ids: list[str]) -> bool:
    """Start the feed insight background thread if not already running."""
    global _feed_insight_thread_running
    with _feed_insight_thread_lock:
        if _feed_insight_thread_running:
            return False
        _feed_insight_thread_running = True

    t = threading.Thread(
        target=_generate_feed_insights_background,
        args=(article_ids,),
        daemon=True,
        name="feed-insight-generator",
    )
    t.start()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# TEXT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _split_paragraphs(text: str) -> list:
    parts = re.split(r"\n{2,}", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _extract_long_sentences(paragraph: str) -> list:
    raw = re.split(r"[.!?]+", paragraph)
    return [
        s.strip().lower()
        for s in raw
        if len(s.strip().split()) >= LONG_SENTENCE_MIN_WORDS
    ]


def _extract_keywords(text: str) -> list:
    text_lower = text.lower().strip()
    words      = re.split(r"[^a-z0-9]+", text_lower)
    words      = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]

    matched_domain_kws = []
    for domain, kws in DOMAIN_KEYWORDS.items():
        for kw in kws:
            if kw in text_lower:
                matched_domain_kws.append(kw)

    if matched_domain_kws:
        seen, unique = set(), []
        for kw in matched_domain_kws:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique[:5]

    unique_words = list(dict.fromkeys(words))
    unique_words.sort(key=len, reverse=True)
    return unique_words[:5]


def _match_domains(text: str) -> list:
    text_lower = text.lower()
    return [
        d for d, kws in DOMAIN_KEYWORDS.items()
        if any(kw in text_lower for kw in kws)
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# PARAGRAPH MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def _paragraph_matches_article(paragraph: str, article: dict) -> bool:
    haystack = " ".join(filter(None, [
        article.get("selected_text", ""),
        article.get("full_content",  ""),
        article.get("content",       ""),
        article.get("title",         ""),
        article.get("short_summary", ""),
    ])).lower()

    if not haystack:
        return False

    long_sentences = _extract_long_sentences(paragraph)

    if not long_sentences:
        kws = _extract_keywords(paragraph)
        return any(kw in haystack for kw in kws)

    for sentence in long_sentences:
        if sentence in haystack:
            return True
    return False


def _all_paragraphs_match(paragraphs: list, article: dict) -> bool:
    for para in paragraphs:
        if not _paragraph_matches_article(para, article):
            return False
    return True


def _search_db(paragraphs: list) -> list:
    log.info("db_search: scanning feed_articles for %d paragraphs", len(paragraphs))
    try:
        docs = (
            db.collection("feed_articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(500).stream()
        )
        all_articles = [d.to_dict() for d in docs]
    except Exception as e:
        log.error("db_search: query failed: %s", e)
        return []

    matched = []
    for art in all_articles:
        if not (art.get("full_content") or art.get("content")):
            continue
        if len(paragraphs) == 1:
            hit = _paragraph_matches_article(paragraphs[0], art)
        else:
            hit = _all_paragraphs_match(paragraphs, art)
        if hit:
            matched.append(art)
            if len(matched) >= MAX_ARTICLES:
                break

    log.info("db_search: found %d matching articles", len(matched))
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# CACHED INSIGHT CHECK (no generation — just check cache)
# ─────────────────────────────────────────────────────────────────────────────

def _get_cached_insight(article: dict) -> Optional[dict]:
    """Return a cached insight if one exists and is valid, otherwise None."""
    # Check article-level cache
    cached = article.get("ai_insight")
    if (
        cached and isinstance(cached, dict)
        and cached.get("summary") and not cached.get("raw")
        and len(cached.get("summary", "")) > 50
    ):
        return cached

    # Check ai_insights collection
    item_id = article.get("id", "")
    try:
        insight_doc = db.collection("ai_insights").document(item_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (isinstance(stored, dict) and stored.get("summary")
                    and not stored.get("raw") and len(stored.get("summary", "")) > 50):
                return stored
    except Exception as e:
        log.warning("insight cache check failed for %s: %s", item_id, e)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class SelectionBody(BaseModel):
    """Body sent when user clicks the Feed button after selecting text."""
    selected_text:     str
    uid:               str = ""
    source_article_id: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/from-selection", status_code=201, summary="Feed from text selection — public")
def feed_from_selection(body: SelectionBody):
    """
    POST /api/feed/from-selection  — **PUBLIC**

    Called when a user selects text and clicks "Feed".

    Returns up to 4 articles. AI insights are generated in the background
    (2–3 minute gap between each). The response includes any insights that
    are already cached; new ones appear as they are generated.

    The frontend should show: *"AI insights are generating — check back in a few minutes."*

    Body:
    ```json
    {
      "selected_text":     "Quantum computing uses superposition...",
      "uid":               "user-uuid",
      "source_article_id": "article-uuid"
    }
    ```

    Response:
    ```json
    {
      "selected_text":    "...",
      "paragraphs":       [...],
      "keywords":         [...],
      "matched_domains":  [...],
      "source":           "database",
      "total":            3,
      "insights_ready":   1,
      "insights_pending": 2,
      "articles": [ { ..., "has_insight": true/false, "ai_insight": {...} or null } ]
    }
    ```
    """
    selected_text = body.selected_text.strip()
    uid           = body.uid.strip()
    source_art_id = body.source_article_id.strip()

    if not selected_text:
        raise HTTPException(400, detail="selected_text is required")
    if len(selected_text) > 5000:
        raise HTTPException(400, detail="selected_text too long (max 5000 chars)")

    # ── Step 1: Parse paragraphs ──────────────────────────────────────────
    paragraphs = _split_paragraphs(selected_text)
    if not paragraphs:
        raise HTTPException(400, detail="Could not parse paragraphs from selection")

    combined_text   = " ".join(paragraphs)
    keywords        = _extract_keywords(combined_text)
    matched_domains = _match_domains(combined_text)
    now_iso         = _now()

    if not keywords:
        raise HTTPException(400, detail="Could not extract keywords from selection")

    log.info("feed/from-selection: uid=%s paragraphs=%d text=%.60s",
             uid, len(paragraphs), selected_text)

    # ── Step 2: Search DB ─────────────────────────────────────────────────
    db_matched  = _search_db(paragraphs)
    data_source = "database"

    if len(db_matched) >= MIN_ARTICLES:
        log.info("feed: DB match OK (%d articles)", len(db_matched))
        articles_to_return = db_matched[:MAX_ARTICLES]
    else:
        log.info("feed: DB match insufficient (%d < %d), fetching RSS",
                 len(db_matched), MIN_ARTICLES)
        data_source  = "rss"
        kws_lower    = [k.lower() for k in keywords]
        raw_articles = []

        for src in RSS_SOURCES:
            try:
                entries = _fetch_rss_raw(src["url"], src["name"], limit=15)
                raw_articles.extend(entries)
            except Exception as e:
                log.warning("RSS failed %s: %s", src["name"], e)

        if not raw_articles:
            if db_matched:
                articles_to_return = db_matched
                data_source = "database_partial"
            else:
                raise HTTPException(502, detail="RSS unavailable and no DB matches found")
        else:
            scored = []
            for art in raw_articles:
                haystack = (art.get("title", "") + " " + art.get("content", "")).lower()
                score    = sum(1 for kw in kws_lower if kw in haystack)
                if score > 0:
                    if combined_text.lower()[:100] in haystack:
                        score += 3
                    scored.append((score, art))

            scored.sort(key=lambda x: x[0], reverse=True)
            rss_picked = [art for _, art in scored[:MAX_ARTICLES]]

            if len(rss_picked) < MIN_ARTICLES:
                broad = [w for w in combined_text.lower().split() if len(w) > 3]
                for art in raw_articles:
                    if art["id"] in {p["id"] for p in rss_picked}:
                        continue
                    h = (art.get("title", "") + " " + art.get("content", "")).lower()
                    if any(w in h for w in broad):
                        rss_picked.append(art)
                    if len(rss_picked) >= MIN_ARTICLES:
                        break

            rss_picked = _deduplicate(rss_picked)[:MAX_ARTICLES]

            enriched = []
            for art in rss_picked:
                try:
                    enriched.append(_enrich_article(art))
                except Exception as e:
                    log.warning("Enrich failed for %s: %s", art.get("id"), e)
                    enriched.append(art)

            for art in enriched:
                tc = (art.get("title", "") + " " + art.get("content", "")).lower()
                art["matched_domains"] = (
                    [d for d, dkws in DOMAIN_KEYWORDS.items() if any(k in tc for k in dkws)]
                    or matched_domains or ["Technology"]
                )

            saved_rss = []
            for art in enriched:
                art.setdefault("id", str(uuid.uuid4()))
                art.update({
                    "feed_source":       "selection",
                    "selected_text":     selected_text,
                    "paragraphs":        paragraphs,
                    "keywords":          keywords,
                    "uid":               uid,
                    "source_article_id": source_art_id,
                    "fetched_at":        now_iso,
                    "has_insight":       False,
                    "table":             "feed_articles",
                })
                try:
                    db.collection("feed_articles").document(art["id"]).set(art, merge=True)
                    saved_rss.append(art)
                except Exception as e:
                    log.error("MongoDB save failed for %s: %s", art["id"], e)

            existing_ids       = {a["id"] for a in db_matched}
            articles_to_return = db_matched + [
                a for a in saved_rss if a["id"] not in existing_ids
            ]
            articles_to_return = articles_to_return[:MAX_ARTICLES]

    # ── Step 3: Ensure paragraphs stored on DB-matched articles ───────────
    for art in articles_to_return:
        if not art.get("paragraphs"):
            try:
                db.collection("feed_articles").document(art["id"]).update({
                    "paragraphs":    paragraphs,
                    "selected_text": selected_text,
                    "updated_at":    now_iso,
                })
                art["paragraphs"]    = paragraphs
                art["selected_text"] = selected_text
            except Exception as e:
                log.warning("Could not update paragraphs for %s: %s", art.get("id"), e)

    # ── Step 4: Attach any already-cached insights ────────────────────────
    ids_needing_insight: list[str] = []
    for art in articles_to_return:
        cached = _get_cached_insight(art)
        if cached:
            art["ai_insight"]  = cached
            art["has_insight"] = True
        else:
            art["ai_insight"]  = None
            art["has_insight"] = False
            ids_needing_insight.append(art["id"])

    # ── Step 5: Start background thread for missing insights ──────────────
    thread_started = False
    if ids_needing_insight:
        thread_started = _start_feed_insight_thread(ids_needing_insight)

    insights_ready   = sum(1 for a in articles_to_return if a.get("has_insight"))
    insights_pending = len(ids_needing_insight)

    log.info(
        "feed done: source=%s returned=%d insights_ready=%d insights_pending=%d thread=%s",
        data_source, len(articles_to_return), insights_ready, insights_pending, thread_started,
    )

    return {
        "selected_text":    selected_text,
        "paragraphs":       paragraphs,
        "keywords":         keywords,
        "matched_domains":  matched_domains,
        "source":           data_source,
        "total":            len(articles_to_return),
        "insights_ready":   insights_ready,
        "insights_pending": insights_pending,
        "insight_message":  (
            "AI insights are generating in the background — "
            "check back in a few minutes."
            if insights_pending > 0 else
            "All insights ready."
        ),
        "articles": articles_to_return,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FEED ITEMS CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/items", summary="List feed articles — public")
def list_feed_items(
    uid:   str = Query("",  description="Filter by user ID"),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        q = db.collection("feed_articles").order_by("fetched_at", direction="DESCENDING")
        if uid.strip():
            q = q.where("uid", "==", uid.strip())
        docs     = q.limit(limit).stream()
        articles = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"articles": articles, "total": len(articles)}


@router.get("/items/{item_id}", summary="Get single feed article — public")
def get_feed_item(item_id: str):
    doc = db.collection("feed_articles").document(item_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Feed article not found")
    return doc.to_dict()


@router.delete("/items/{item_id}", summary="Delete feed article — requires auth")
def delete_feed_item(item_id: str, payload: dict = Depends(require_auth)):
    doc_ref = db.collection("feed_articles").document(item_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Feed article not found")
    try:
        doc_ref.delete()
        db.collection("ai_insights").document(item_id).delete()
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    log.info("feed deleted: %s by %s", item_id, get_uid(payload))
    return {"deleted": True, "article_id": item_id}


def _cleanup_old_feed(older_than_days: int = 15) -> dict:
    """
    Core cleanup logic — callable from the endpoint or a scheduler.

    Scans `feed_articles` for documents whose `fetched_at` timestamp is older
    than `older_than_days` days, then deletes:
      • the `feed_articles` document
      • the matching `ai_insights` document (same ID)

    Returns a summary dict with counts of deleted and failed documents.
    """
    cutoff: datetime = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    cutoff_iso: str  = cutoff.isoformat()

    log.info("feed_cleanup: scanning for articles fetched before %s", cutoff_iso)

    try:
        docs = (
            db.collection("feed_articles")
              .where("fetched_at", "<", cutoff_iso)
              .stream()
        )
        old_docs = list(docs)
    except Exception as e:
        log.error("feed_cleanup: query failed: %s", e)
        raise

    if not old_docs:
        log.info("feed_cleanup: nothing to delete (cutoff=%s)", cutoff_iso)
        return {"deleted": 0, "failed": 0, "cutoff": cutoff_iso}

    log.info("feed_cleanup: found %d stale articles to delete", len(old_docs))

    deleted = 0
    failed  = 0

    for doc in old_docs:
        article_id = doc.id
        try:
            db.collection("feed_articles").document(article_id).delete()
            # Always attempt insight deletion — fails silently if absent
            db.collection("ai_insights").document(article_id).delete()
            log.info("feed_cleanup: deleted %s", article_id)
            deleted += 1
        except Exception as e:
            log.error("feed_cleanup: failed to delete %s: %s", article_id, e)
            failed += 1

    log.info(
        "feed_cleanup: done — deleted=%d failed=%d older_than_days=%d",
        deleted, failed, older_than_days,
    )
    return {"deleted": deleted, "failed": failed, "cutoff": cutoff_iso}


@router.delete(
    "/cleanup",
    summary="Delete feed articles and their insights older than N days — MOD/ADMIN ONLY",
)
def cleanup_old_feed_items(
    days:    int  = Query(15, ge=1, le=365, description="Delete articles older than this many days"),
    dry_run: bool = Query(False,            description="true = count matches without deleting"),
    payload: dict = Depends(require_mod),
):
    """
    DELETE /api/feed/cleanup  — **MOD/ADMIN ONLY**

    Removes stale feed articles and their corresponding AI insights.

    - `days`    — age threshold in days (default 15, min 1, max 365).
    - `dry_run` — when `true`, returns the count of articles that *would* be
                  deleted without touching the database.

    Both `feed_articles` and `ai_insights` documents are removed for each
    matched article so no orphaned insights are left behind.
    """
    cutoff     = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    if dry_run:
        try:
            docs  = db.collection("feed_articles").where("fetched_at", "<", cutoff_iso).stream()
            count = sum(1 for _ in docs)
        except Exception as e:
            raise HTTPException(500, detail=str(e))
        log.info(
            "feed_cleanup dry_run: %d articles would be deleted (days=%d) by %s",
            count, days, get_uid(payload),
        )
        return {"dry_run": True, "would_delete": count, "cutoff": cutoff_iso}

    try:
        result = _cleanup_old_feed(older_than_days=days)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("feed_cleanup: triggered by mod/admin=%s", get_uid(payload))
    return {
        "dry_run":          False,
        "deleted":          result["deleted"],
        "failed":           result["failed"],
        "cutoff":           result["cutoff"],
        "older_than_days":  days,
    }


@router.post(
    "/items/{item_id}/insight",
    summary="Manually generate insight for a feed article — MOD/ADMIN ONLY",
)
def feed_item_insight(
    item_id: str,
    force:   bool = Query(False, description="true = skip cache and regenerate"),
    payload: dict = Depends(require_mod),   # MOD/ADMIN ONLY
):
    """
    POST /api/feed/items/{item_id}/insight  — **MOD/ADMIN ONLY**

    Manually generate (or re-generate) an AI insight for a feed article.
    Use this only to fix articles whose automatic insight generation failed.

    Normally, insights are generated automatically after feed/from-selection.

    Pass ?force=true to bypass the cache and regenerate.
    """
    doc_ref = db.collection("feed_articles").document(item_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Feed article not found")

    article = doc.to_dict()

    # Cache check (unless force)
    if not force:
        cached = _get_cached_insight(article)
        if cached:
            # Insight exists but feed_articles may be out of sync (has_insight=False).
            # Repair now so GET /api/feed/items returns has_insight=True consistently.
            if not article.get("has_insight"):
                log.info("feed_item_insight: repairing out-of-sync has_insight for %s", item_id)
                try:
                    doc_ref.set({
                        "ai_insight": cached, "has_insight": True,
                        "insight_generated_at": _now(),
                    }, merge=True)
                except Exception as e:
                    log.error("feed_item_insight: repair save failed %s: %s", item_id, e)
            return {"article_id": item_id, "ai_insight": cached, "cached": True}

    # Generate insight synchronously (admin initiated)
    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        log.error("feed_item_insight: generate failed %s: %s", item_id, e)
        raise HTTPException(502, detail=str(e))

    # Save to feed_articles
    try:
        doc_ref.set({
            "ai_insight": insight, "has_insight": True,
            "insight_generated_at": _now(),
        }, merge=True)
        log.info("feed_item_insight: saved to feed_articles %s", item_id)
    except Exception as e:
        log.error("feed_item_insight: feed_articles save failed %s: %s", item_id, e)
        raise HTTPException(500, detail=f"Insight generated but failed to save: {e}")

    # Save to ai_insights collection
    try:
        db.collection("ai_insights").document(item_id).set({
            "article_id":      item_id,
            "source_table":    "feed_articles",
            "title":           article.get("title", ""),
            "topic":           (article.get("matched_domains") or ["Technology"])[0],
            "source":          article.get("source", ""),
            "matched_domains": article.get("matched_domains", []),
            "article_url":     article.get("article_url") or article.get("url", ""),
            "keywords":        article.get("keywords", []),
            "selected_text":   article.get("selected_text", ""),
            "ai_insight":      insight,
            "created_at":      _now(),
        })
    except Exception as e:
        log.error("feed_item_insight: ai_insights save failed %s: %s", item_id, e)

    log.info("feed_item_insight: done %s by mod/admin=%s", item_id, get_uid(payload))
    return {"article_id": item_id, "ai_insight": insight, "cached": False}