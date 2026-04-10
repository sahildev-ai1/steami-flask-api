"""
routers/feed.py  —  Feed API  v8
==================================
Flow when user selects text and clicks Feed:

  1. Split selected_text into paragraphs (double-newline separated).
  2. For EACH paragraph, search `feed_articles` MongoDB collection for
     articles containing at least one FULL LONG SENTENCE (≥8 words) match.
     - Multi-paragraph selection: every paragraph must match the same article.
  3. If ≥ MIN_ARTICLES (2) DB matches found → return those (no RSS needed).
  4. Otherwise fall back to RSS: score by keywords, pick 2-7 articles,
     enrich (image/summary/full_content), save to `feed_articles`.
  5. Generate AI insight for every returned article (cached or fresh Gemini).
  6. Return 2-7 articles with ai_insight inline.

ENDPOINTS:
  POST   /api/feed/from-selection         — main pipeline (public)
  GET    /api/feed/items                  — list feed articles (public)
  GET    /api/feed/items/{id}             — single feed article (public)
  DELETE /api/feed/items/{id}             — delete (requires auth)
  POST   /api/feed/items/{id}/insight     — get/generate insight (requires auth)
"""

import uuid
import re
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from mongodb_client import db
from gemini_client import generate_ai_insight
from auth import require_auth, get_uid
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

MIN_ARTICLES = 2   # minimum articles to return (2 is fine)
MAX_ARTICLES = 7   # maximum articles to return

# A sentence needs at least this many words to count as a "long sentence"
LONG_SENTENCE_MIN_WORDS = 8

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
# TEXT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _split_paragraphs(text: str) -> list:
    """
    Split selected text into paragraphs separated by one or more blank lines.
    Returns a list of non-empty stripped paragraph strings.
    Single selections with no blank lines return a list with one item.
    """
    parts = re.split(r"\n{2,}", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _extract_long_sentences(paragraph: str) -> list:
    """
    Extract sentences with at least LONG_SENTENCE_MIN_WORDS words.
    Splits on . ! ? boundaries.
    Returns lowercase stripped sentences.
    """
    raw = re.split(r"[.!?]+", paragraph)
    return [
        s.strip().lower()
        for s in raw
        if len(s.strip().split()) >= LONG_SENTENCE_MIN_WORDS
    ]


def _extract_keywords(text: str) -> list:
    """
    Extract up to 5 meaningful keywords from text.
    Prefers domain keywords; falls back to longest non-stop-word tokens.
    """
    text_lower = text.lower().strip()
    words      = re.split(r"[^a-z0-9]+", text_lower)
    words      = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]

    # Domain keyword matches (most specific)
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
    """Return domain names whose keywords appear in text."""
    text_lower = text.lower()
    return [
        d for d, kws in DOMAIN_KEYWORDS.items()
        if any(kw in text_lower for kw in kws)
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# PARAGRAPH MATCHING  —  search existing feed_articles in MongoDB
# ─────────────────────────────────────────────────────────────────────────────

def _paragraph_matches_article(paragraph: str, article: dict) -> bool:
    """
    Returns True if at least one long sentence from `paragraph`
    appears as a substring in the stored article text fields.

    Checks: selected_text, full_content, content, title, short_summary.

    If paragraph is too short to produce long sentences,
    falls back to keyword matching.
    """
    # Build searchable haystack from all stored text fields
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
        # Too short — fall back to keyword match
        kws = _extract_keywords(paragraph)
        return any(kw in haystack for kw in kws)

    # At least one long sentence must appear in the article
    for sentence in long_sentences:
        if sentence in haystack:
            return True
    return False


def _all_paragraphs_match(paragraphs: list, article: dict) -> bool:
    """
    For multi-paragraph selections:
    Returns True only if EVERY paragraph has at least one matching long sentence
    in the article. This ensures the article is truly relevant to the whole selection.
    """
    for para in paragraphs:
        if not _paragraph_matches_article(para, article):
            return False  # one paragraph has no match → article not relevant
    return True


def _search_db(paragraphs: list) -> list:
    """
    Scan `feed_articles` collection for articles matching the paragraphs.
    Returns up to MAX_ARTICLES matching article dicts.

    Uses in-memory scan because the long-sentence matching logic
    cannot be expressed as a simple MongoDB query filter.
    Scans up to 500 recent articles.
    """
    log.info("db_search: scanning feed_articles for %d paragraphs", len(paragraphs))

    try:
        docs = (
            db.collection("feed_articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(500)
              .stream()
        )
        all_articles = [d.to_dict() for d in docs]
    except Exception as e:
        log.error("db_search: query failed: %s", e)
        return []

    matched = []
    for art in all_articles:
        if not (art.get("full_content") or art.get("content")):
            continue   # skip articles with no content

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
# AI INSIGHT HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_generate_insight(article: dict) -> Optional[dict]:
    """
    Return a cached insight if one exists and is valid, otherwise generate
    a new one via Gemini and save it to both feed_articles and ai_insights.
    Returns the insight dict or None if generation fails.
    """
    item_id = article.get("id", "")
    now_iso = _now()

    # Check article-level cache
    cached = article.get("ai_insight")
    if (
        cached and isinstance(cached, dict)
        and cached.get("summary") and not cached.get("raw")
        and len(cached.get("summary", "")) > 50
    ):
        return cached

    # Check ai_insights collection cache
    try:
        insight_doc = db.collection("ai_insights").document(item_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (isinstance(stored, dict) and stored.get("summary")
                    and not stored.get("raw") and len(stored.get("summary","")) > 50):
                return stored
    except Exception as e:
        log.warning("insight cache check failed for %s: %s", item_id, e)

    # Generate via Gemini
    try:
        insight = generate_ai_insight(article)
        log.info("insight generated: %s domain=%s words=%d",
                 item_id, insight.get("domain","?"),
                 len(insight.get("summary","").split()))
    except Exception as e:
        log.error("Gemini failed for %s: %s", item_id, e)
        return None

    # Save to feed article doc
    try:
        db.collection("feed_articles").document(item_id).update({
            "ai_insight": insight, "has_insight": True,
            "insight_generated_at": now_iso,
        })
    except Exception as e:
        log.error("insight: feed_articles update failed for %s: %s", item_id, e)

    # Save to shared ai_insights collection
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
            "created_at":      now_iso,
        })
    except Exception as e:
        log.error("insight: ai_insights save failed for %s: %s", item_id, e)

    return insight


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class SelectionBody(BaseModel):
    """Body sent when user clicks the Feed button after selecting text."""
    selected_text:     str        # highlighted text (single or multi-paragraph)
    uid:               str = ""   # user ID (optional, for tagging saved articles)
    source_article_id: str = ""   # ID of the article being read (optional)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/from-selection", status_code=201, summary="Feed from text selection — public")
def feed_from_selection(body: SelectionBody):
    """
    POST /api/feed/from-selection
    Main feed pipeline. Called when user selects text and clicks Feed.

    Returns 2-7 articles with AI insights included.

    Body:
    {
      "selected_text":     "Quantum computing uses superposition...\\n\\nIBM research shows...",
      "uid":               "user-uuid",
      "source_article_id": "article-uuid"
    }

    Response:
    {
      "selected_text":   "...",
      "paragraphs":      ["Quantum computing...", "IBM research..."],
      "keywords":        ["quantum", "computing"],
      "matched_domains": ["PHYSICS"],
      "source":          "database",
      "total":           4,
      "articles": [
        {
          "id":            "...",
          "title":         "...",
          "short_summary": "...",
          "image_url":     "...",
          "article_url":   "...",
          "has_insight":   true,
          "ai_insight":    { "summary": "...", "svg": "...", ... }
        }, ...
      ]
    }

    curl -X POST http://127.0.0.1:5000/api/feed/from-selection \\
      -H "Content-Type: application/json" \\
      -d '{"selected_text":"quantum computing breaks encryption","uid":"user123"}'
    """
    selected_text = body.selected_text.strip()
    uid           = body.uid.strip()
    source_art_id = body.source_article_id.strip()

    if not selected_text:
        raise HTTPException(400, detail="selected_text is required")
    if len(selected_text) > 5000:
        raise HTTPException(400, detail="selected_text too long (max 5000 chars)")

    # ── Step 1: Split into paragraphs ─────────────────────────────────────
    paragraphs = _split_paragraphs(selected_text)
    if not paragraphs:
        raise HTTPException(400, detail="Could not parse paragraphs from selection")

    combined_text   = " ".join(paragraphs)
    keywords        = _extract_keywords(combined_text)
    matched_domains = _match_domains(combined_text)
    now_iso         = _now()

    log.info("feed/from-selection: uid=%s paragraphs=%d text=%.60s",
             uid, len(paragraphs), selected_text)

    if not keywords:
        raise HTTPException(400, detail="Could not extract keywords from selection")

    # ── Step 2: Search MongoDB for paragraph matches ──────────────────────
    db_matched  = _search_db(paragraphs)
    data_source = "database"

    if len(db_matched) >= MIN_ARTICLES:
        # Enough DB matches — no RSS needed
        log.info("feed: DB match OK (%d articles), skipping RSS", len(db_matched))
        articles_to_return = db_matched[:MAX_ARTICLES]

    else:
        # Fall back to RSS fetch
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
            # No RSS — return what DB had (even if < MIN)
            if db_matched:
                articles_to_return = db_matched
                data_source = "database_partial"
            else:
                raise HTTPException(502, detail="RSS unavailable and no DB matches found")
        else:
            # Score RSS articles by keyword density
            scored = []
            for art in raw_articles:
                haystack = (art.get("title","") + " " + art.get("content","")).lower()
                score    = sum(1 for kw in kws_lower if kw in haystack)
                if score > 0:
                    if combined_text.lower()[:100] in haystack:
                        score += 3
                    scored.append((score, art))

            scored.sort(key=lambda x: x[0], reverse=True)
            rss_picked = [art for _, art in scored[:MAX_ARTICLES]]

            # Broad fallback if still < MIN
            if len(rss_picked) < MIN_ARTICLES:
                broad = [w for w in combined_text.lower().split() if len(w) > 3]
                for art in raw_articles:
                    if art["id"] in {p["id"] for p in rss_picked}:
                        continue
                    h = (art.get("title","") + " " + art.get("content","")).lower()
                    if any(w in h for w in broad):
                        rss_picked.append(art)
                    if len(rss_picked) >= MIN_ARTICLES:
                        break

            rss_picked = _deduplicate(rss_picked)[:MAX_ARTICLES]

            # Enrich (fetch page for image, summary, full_content)
            enriched = []
            for art in rss_picked:
                try:
                    enriched.append(_enrich_article(art))
                except Exception as e:
                    log.warning("Enrich failed for %s: %s", art.get("id"), e)
                    enriched.append(art)

            # Tag matched domains
            for art in enriched:
                tc = (art.get("title","") + " " + art.get("content","")).lower()
                art["matched_domains"] = (
                    [d for d, dkws in DOMAIN_KEYWORDS.items()
                     if any(k in tc for k in dkws)]
                    or matched_domains or ["Technology"]
                )

            # Save new RSS articles to MongoDB with selected_text + paragraphs stored
            saved_rss = []
            for art in enriched:
                art.setdefault("id", str(uuid.uuid4()))
                art.update({
                    "feed_source":       "selection",
                    "selected_text":     selected_text,   # store the user's full selection
                    "paragraphs":        paragraphs,       # store individual paragraphs
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

            # Merge DB matches with new RSS articles
            existing_ids       = {a["id"] for a in db_matched}
            articles_to_return = db_matched + [
                a for a in saved_rss if a["id"] not in existing_ids
            ]
            articles_to_return = articles_to_return[:MAX_ARTICLES]

    # ── Step 3: Ensure paragraphs are stored on DB-matched articles ───────
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

    # ── Step 4: Generate AI insights for all articles ─────────────────────
    for art in articles_to_return:
        insight = _get_or_generate_insight(art)
        if insight:
            art["ai_insight"]  = insight
            art["has_insight"] = True

    log.info("feed done: source=%s paragraphs=%d returned=%d insights=%d",
             data_source, len(paragraphs), len(articles_to_return),
             sum(1 for a in articles_to_return if a.get("has_insight")))

    return {
        "selected_text":   selected_text,
        "paragraphs":      paragraphs,
        "keywords":        keywords,
        "matched_domains": matched_domains,
        "source":          data_source,
        "total":           len(articles_to_return),
        "articles":        articles_to_return,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FEED ITEMS CRUD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/items", summary="List feed articles — public")
def list_feed_items(
    uid:   str = Query("",  description="Filter by user ID"),
    limit: int = Query(20, ge=1, le=100),
):
    """
    GET /api/feed/items?uid=user123&limit=20

    curl "http://127.0.0.1:5000/api/feed/items?uid=user123"
    """
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
    """
    GET /api/feed/items/{item_id}

    curl http://127.0.0.1:5000/api/feed/items/ITEM_ID
    """
    doc = db.collection("feed_articles").document(item_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Feed article not found")
    return doc.to_dict()


@router.delete("/items/{item_id}", summary="Delete feed article — requires auth")
def delete_feed_item(item_id: str, payload: dict = Depends(require_auth)):
    """
    DELETE /api/feed/items/{item_id}
    Also deletes the cached AI insight.

    curl -X DELETE http://127.0.0.1:5000/api/feed/items/ITEM_ID \\
      -H "Authorization: Bearer <token>"
    """
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


@router.post("/items/{item_id}/insight", summary="Get or generate insight — requires auth")
def feed_item_insight(
    item_id: str,
    force:   bool = Query(False),
    payload: dict = Depends(require_auth),
):
    """
    POST /api/feed/items/{item_id}/insight
    Pass ?force=true to bypass cache.

    curl -X POST http://127.0.0.1:5000/api/feed/items/ITEM_ID/insight \\
      -H "Authorization: Bearer <token>"
    """
    doc_ref = db.collection("feed_articles").document(item_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Feed article not found")

    article = doc.to_dict()
    now_iso = _now()

    if not force:
        cached = article.get("ai_insight")
        if (cached and isinstance(cached, dict) and cached.get("summary")
                and not cached.get("raw") and len(cached.get("summary","")) > 50):
            return {"article_id": item_id, "source_table": "feed_articles",
                    "ai_insight": cached, "cached": True}

        insight_doc = db.collection("ai_insights").document(item_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (isinstance(stored, dict) and stored.get("summary")
                    and not stored.get("raw") and len(stored.get("summary","")) > 50):
                return {"article_id": item_id, "source_table": "feed_articles",
                        "ai_insight": stored, "cached": True}

    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        raise HTTPException(502, detail=str(e))

    try:
        doc_ref.update({"ai_insight": insight, "has_insight": True,
                        "insight_generated_at": now_iso})
    except Exception as e:
        log.error("feed insight update failed: %s", e)

    try:
        db.collection("ai_insights").document(item_id).set({
            "article_id": item_id, "source_table": "feed_articles",
            "title": article.get("title",""),
            "topic": (article.get("matched_domains") or ["Technology"])[0],
            "source": article.get("source",""),
            "matched_domains": article.get("matched_domains",[]),
            "article_url": article.get("article_url") or article.get("url",""),
            "keywords": article.get("keywords",[]),
            "selected_text": article.get("selected_text",""),
            "ai_insight": insight, "created_at": now_iso,
        })
    except Exception as e:
        log.error("feed ai_insights save failed: %s", e)

    return {"article_id": item_id, "source_table": "feed_articles",
            "ai_insight": insight, "cached": False}