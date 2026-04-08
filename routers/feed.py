"""
Feed router — /api/feed/...
Ported from Flask Blueprint to FastAPI APIRouter.
"""

import uuid
import re
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from firestore_client import db
from gemini_client import generate_ai_insight
# Auth — only insight endpoint is locked; feed browsing is public
from auth import require_auth, get_uid
from article_fetcher import (
    RSS_SOURCES,
    DOMAIN_KEYWORDS,
    _fetch_rss_raw,
    _enrich_article,
    _deduplicate,
)

log = logging.getLogger(__name__)
router = APIRouter()

_STOP_WORDS = {
    "the","a","an","and","or","but","in","on","at","to","for","of","with",
    "is","was","are","were","be","been","being","have","has","had","do","does",
    "did","will","would","could","should","may","might","shall","can",
    "this","that","these","those","it","its","i","we","you","he","she","they",
    "not","no","so","if","as","by","from","up","out","about","into","than",
    "then","there","when","where","who","which","what","how","all","any",
    "both","each","few","more","most","other","some","such","very","just",
}


def _extract_keywords(text: str) -> list[str]:
    text_lower = text.lower().strip()
    words      = re.split(r"[^a-z0-9]+", text_lower)
    words      = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]

    matched_domain_kws: list[str] = []
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


def _match_domains(text: str) -> list[str]:
    text_lower = text.lower()
    return [
        domain for domain, kws in DOMAIN_KEYWORDS.items()
        if any(kw in text_lower for kw in kws)
    ]


# ── Request bodies ─────────────────────────────────────────────────────────

class SelectionBody(BaseModel):
    selected_text:     str
    uid:               str = ""
    source_article_id: str = ""


# ══════════════════════════════════════════════════════════════════════════
# FROM SELECTION
# ══════════════════════════════════════════════════════════════════════════

@router.post("/from-selection", status_code=201)
def feed_from_selection(body: SelectionBody):
    """
    Text selection → keyword extraction → RSS fetch → save 2-8 articles.
    Called when user selects text and clicks the Feed button.
    """
    selected_text = body.selected_text.strip()
    uid           = body.uid.strip()
    source_art_id = body.source_article_id.strip()

    if not selected_text:
        raise HTTPException(400, detail="selected_text is required")
    if len(selected_text) > 2000:
        raise HTTPException(400, detail="selected_text too long (max 2000 chars)")

    keywords        = _extract_keywords(selected_text)
    matched_domains = _match_domains(selected_text)
    kws_lower       = [k.lower() for k in keywords]

    log.info("feed/from-selection: text=%.60s  keywords=%s  domains=%s",
             selected_text, keywords, matched_domains)

    if not keywords:
        raise HTTPException(400, detail="Could not extract keywords from selected text")

    raw_articles: list[dict] = []
    for src in RSS_SOURCES:
        try:
            entries = _fetch_rss_raw(src["url"], src["name"], limit=15)
            raw_articles.extend(entries)
        except Exception as e:
            log.warning("RSS feed failed %s: %s", src["name"], e)

    if not raw_articles:
        raise HTTPException(502, detail="All RSS sources failed — try again later")

    scored: list[tuple[int, dict]] = []
    for art in raw_articles:
        haystack = (art.get("title", "") + " " + art.get("content", "")).lower()
        score    = sum(1 for kw in kws_lower if kw in haystack)
        if score > 0:
            if selected_text.lower() in haystack:
                score += 3
            scored.append((score, art))

    scored.sort(key=lambda x: x[0], reverse=True)
    MIN_ARTICLES, MAX_ARTICLES = 2, 8
    picked = [art for _, art in scored[:MAX_ARTICLES]]

    if len(picked) < MIN_ARTICLES:
        broad_words = [w for w in re.split(r"\s+", selected_text.lower()) if len(w) > 3]
        for art in raw_articles:
            if art["id"] in {p["id"] for p in picked}:
                continue
            haystack = (art.get("title", "") + " " + art.get("content", "")).lower()
            if any(w in haystack for w in broad_words):
                picked.append(art)
            if len(picked) >= MIN_ARTICLES:
                break

    if not picked:
        return {
            "selected_text": selected_text, "keywords": keywords,
            "matched_domains": matched_domains, "saved": 0, "articles": [],
            "message": "No articles found matching this selection.",
        }

    picked = _deduplicate(picked)[:MAX_ARTICLES]

    enriched: list[dict] = []
    for art in picked:
        try:
            enriched.append(_enrich_article(art))
        except Exception as e:
            log.warning("Enrich failed for %s: %s", art.get("id"), e)
            enriched.append(art)

    now_iso = datetime.now(timezone.utc).isoformat()
    for art in enriched:
        text_check = (art.get("title", "") + " " + art.get("content", "")).lower()
        art["matched_domains"] = [
            d for d, dkws in DOMAIN_KEYWORDS.items()
            if any(k in text_check for k in dkws)
        ] or matched_domains or ["Technology"]

    saved: list[dict] = []
    for art in enriched:
        art.setdefault("id", str(uuid.uuid4()))
        art.update({
            "feed_source":       "selection",
            "selected_text":     selected_text,
            "keywords":          keywords,
            "uid":               uid,
            "source_article_id": source_art_id,
            "fetched_at":        now_iso,
            "has_insight":       False,
            "table":             "feed_articles",
        })
        try:
            db.collection("feed_articles").document(art["id"]).set(art, merge=True)
            saved.append(art)
        except Exception as e:
            log.error("Firestore save failed for feed article %s: %s", art["id"], e)

    log.info("feed/from-selection done: keywords=%s  raw=%d  picked=%d  saved=%d",
             keywords, len(raw_articles), len(picked), len(saved))

    return {
        "selected_text": selected_text, "keywords": keywords,
        "matched_domains": matched_domains, "saved": len(saved), "articles": saved,
    }


# ══════════════════════════════════════════════════════════════════════════
# FEED ITEMS CRUD
# ══════════════════════════════════════════════════════════════════════════

@router.get("/items")
def list_feed_items(
    uid:   str = Query(""),
    limit: int = Query(20, ge=1, le=100),
):
    """List saved feed articles, newest first. Optional uid filter."""
    try:
        q = db.collection("feed_articles").order_by("fetched_at", direction="DESCENDING")
        if uid.strip():
            q = q.where("uid", "==", uid.strip())
        docs     = q.limit(limit).stream()
        articles = [d.to_dict() for d in docs]
    except Exception as e:
        log.error("list_feed_items failed: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"articles": articles, "total": len(articles)}


@router.get("/items/{item_id}")
def get_feed_item(item_id: str):
    """Get a single feed article by ID (includes full_content)."""
    doc = db.collection("feed_articles").document(item_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Feed article not found")
    return doc.to_dict()


@router.delete("/items/{item_id}")
def delete_feed_item(item_id: str):
    """Delete a feed article and its cached insight."""
    doc_ref = db.collection("feed_articles").document(item_id)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="Feed article not found")
    try:
        doc_ref.delete()
        db.collection("ai_insights").document(item_id).delete()
    except Exception as e:
        log.error("delete_feed_item failed: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"deleted": True, "article_id": item_id}


@router.post("/items/{item_id}/insight")
def feed_item_insight(
    item_id: str,
    force:   bool = Query(False),
    payload: dict = Depends(require_auth),  # locked — any logged-in user
):
    """Generate AI insight for a feed article. Pass ?force=true to skip cache."""
    now_iso = datetime.now(timezone.utc).isoformat()
    doc_ref = db.collection("feed_articles").document(item_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Feed article not found")

    article = doc.to_dict()

    if not force:
        cached = article.get("ai_insight")
        if (
            cached and isinstance(cached, dict)
            and cached.get("summary") and not cached.get("raw")
            and len(cached.get("summary", "")) > 50
        ):
            return {"article_id": item_id, "source_table": "feed_articles",
                    "ai_insight": cached, "cached": True}

        insight_doc = db.collection("ai_insights").document(item_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (isinstance(stored, dict) and stored.get("summary")
                    and not stored.get("raw") and len(stored.get("summary", "")) > 50):
                return {"article_id": item_id, "source_table": "feed_articles",
                        "ai_insight": stored, "cached": True}

    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        log.error("Gemini error for feed article %s: %s", item_id, e)
        raise HTTPException(502, detail=str(e))

    try:
        doc_ref.update({"ai_insight": insight, "has_insight": True,
                        "insight_generated_at": now_iso})
    except Exception as e:
        log.error("Failed to update feed article: %s", e)

    try:
        db.collection("ai_insights").document(item_id).set({
            "article_id":    item_id, "source_table": "feed_articles",
            "title":         article.get("title", ""),
            "topic":         (article.get("matched_domains") or ["Technology"])[0],
            "source":        article.get("source", ""),
            "matched_domains": article.get("matched_domains", []),
            "article_url":   article.get("article_url") or article.get("url", ""),
            "keywords":      article.get("keywords", []),
            "selected_text": article.get("selected_text", ""),
            "ai_insight":    insight, "created_at": now_iso,
        })
    except Exception as e:
        log.error("Failed to save ai_insights doc for feed article: %s", e)

    log.info("Feed insight generated: %s  domain=%s  words=%d",
             item_id, insight.get("domain", "?"), len(insight.get("summary", "").split()))
    return {"article_id": item_id, "source_table": "feed_articles",
            "ai_insight": insight, "cached": False}