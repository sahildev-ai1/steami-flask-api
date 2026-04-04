"""
STEAMI Feed API  — feed.py
Blueprint: /api/feed/...

Handles the "select text → Feed button" feature.

FLOW
────
1. User selects text (word / sentence / paragraph) on frontend
2. Frontend sends selected text to POST /api/feed/from-selection
3. Backend extracts keywords from the text
4. Searches ALL RSS sources for articles matching those keywords
5. Saves 2–8 matched articles into `feed_articles` Firestore collection
   (separate table from `articles`) with the selected_text + keywords stored
6. Returns saved articles with short_summary + image_url to frontend

ENDPOINTS
─────────────────────────────────────────────────────────────────────
POST  /api/feed/from-selection       — main: text → keywords → fetch → save → return
GET   /api/feed/items                — list saved feed articles (newest first)
GET   /api/feed/items/<id>           — single feed article
POST  /api/feed/items/<id>/insight   — generate AI insight for a feed article
DELETE /api/feed/items/<id>          — delete a feed article
─────────────────────────────────────────────────────────────────────
"""

import uuid
import re
import logging
from datetime import datetime, timezone

import requests as http_requests
from bs4 import BeautifulSoup
from flask import Blueprint, request, jsonify

from firestore_client import db
from gemini_client import generate_ai_insight
from article_fetcher import (
    RSS_SOURCES,
    DOMAIN_KEYWORDS,
    _fetch_rss_raw,
    _enrich_article,
    _deduplicate,
)

log = logging.getLogger(__name__)
feed_bp = Blueprint("feed", __name__, url_prefix="/api/feed")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/123.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────────────────────────────────────
# Keyword extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

# Common stop words to strip from selected text before keyword matching
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
    """
    Extract meaningful search keywords from selected text.

    Strategy:
    1. Lowercase + split on non-alphanumeric
    2. Remove stop words and single characters
    3. Check against DOMAIN_KEYWORDS to find domain matches
    4. If matched domain keywords exist → use those (most specific)
    5. Otherwise fall back to the longest non-stop words (up to 5)

    Returns a list of 1–5 keyword strings.
    """
    text_lower = text.lower().strip()
    words      = re.split(r"[^a-z0-9]+", text_lower)
    words      = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]

    # Check for domain keyword matches (multi-word first, then single)
    matched_domain_kws: list[str] = []
    for domain, kws in DOMAIN_KEYWORDS.items():
        for kw in kws:
            if kw in text_lower:
                matched_domain_kws.append(kw)

    if matched_domain_kws:
        # Deduplicate, keep up to 5
        seen, unique = set(), []
        for kw in matched_domain_kws:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique[:5]

    # Fall back: return longest unique content words
    unique_words = list(dict.fromkeys(words))   # preserve order, dedup
    unique_words.sort(key=len, reverse=True)
    return unique_words[:5]


def _match_domains(text: str) -> list[str]:
    """Return domain names whose keywords appear in text."""
    text_lower = text.lower()
    return [
        domain for domain, kws in DOMAIN_KEYWORDS.items()
        if any(kw in text_lower for kw in kws)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/feed/from-selection
# ─────────────────────────────────────────────────────────────────────────────

@feed_bp.route("/from-selection", methods=["POST"])
def feed_from_selection():
    """
    POST /api/feed/from-selection

    Called when the user selects text and clicks the "Feed" button.

    Body:
    {
      "selected_text": "machine learning models are transforming healthcare",
      "uid":           "user123",        // optional — for personalisation later
      "source_article_id": "uuid"        // optional — which article they were reading
    }

    What it does:
    1. Extracts keywords from selected_text
    2. Fetches articles from all 8 RSS sources matching those keywords
    3. Scores articles by keyword density, picks 2–8 best
    4. Enriches each (image, short_summary, full_content)
    5. Saves to `feed_articles` Firestore collection
       (separate from `articles` — never mixed up)
    6. Returns saved articles

    Response:
    {
      "selected_text":  "machine learning models are transforming healthcare",
      "keywords":       ["machine learning", "healthcare", "models"],
      "matched_domains":["AI", "Biology/Medicine"],
      "saved":          4,
      "articles": [
        {
          "id":              "uuid",
          "title":           "...",
          "short_summary":   "30-40 word summary...",
          "image_url":       "https://...",
          "article_url":     "https://...",
          "matched_domains": ["AI"],
          "source":          "MIT Technology Review",
          "published_at":    "...",
          "fetched_at":      "...",
          "has_insight":     false,
          "feed_source":     "selection",
          "selected_text":   "machine learning models...",
          "keywords":        ["machine learning", ...]
        }, ...
      ]
    }

    curl -X POST http://127.0.0.1:5000/api/feed/from-selection \\
      -H "Content-Type: application/json" \\
      -d '{"selected_text":"quantum computing breaking encryption","uid":"user123"}'
    """
    data          = request.get_json(silent=True) or {}
    selected_text = (data.get("selected_text") or "").strip()
    uid           = (data.get("uid") or "").strip()
    source_art_id = (data.get("source_article_id") or "").strip()

    if not selected_text:
        return jsonify({"error": "selected_text is required"}), 400
    if len(selected_text) > 2000:
        return jsonify({"error": "selected_text too long (max 2000 chars)"}), 400

    # ── Step 1: Extract keywords ───────────────────────────────────────────
    keywords       = _extract_keywords(selected_text)
    matched_domains = _match_domains(selected_text)
    kws_lower      = [k.lower() for k in keywords]

    log.info(
        "feed/from-selection: text=%.60s  keywords=%s  domains=%s",
        selected_text, keywords, matched_domains,
    )

    if not keywords:
        return jsonify({"error": "Could not extract keywords from selected text"}), 400

    # ── Step 2: Fetch from all RSS sources ────────────────────────────────
    raw_articles: list[dict] = []
    for src in RSS_SOURCES:
        try:
            entries = _fetch_rss_raw(src["url"], src["name"], limit=15)
            raw_articles.extend(entries)
        except Exception as e:
            log.warning("RSS feed failed %s: %s", src["name"], e)

    if not raw_articles:
        return jsonify({"error": "All RSS sources failed — try again later"}), 502

    # ── Step 3: Score articles by keyword match ────────────────────────────
    scored: list[tuple[int, dict]] = []
    for art in raw_articles:
        haystack = (art.get("title", "") + " " + art.get("content", "")).lower()
        score    = sum(1 for kw in kws_lower if kw in haystack)
        if score > 0:
            # Bonus: exact phrase match
            if selected_text.lower() in haystack:
                score += 3
            scored.append((score, art))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Take 2–8 articles
    MIN_ARTICLES = 2
    MAX_ARTICLES = 8

    picked = [art for _, art in scored[:MAX_ARTICLES]]

    if len(picked) < MIN_ARTICLES:
        # Fallback: broaden — use individual words from selected text
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
        return jsonify({
            "selected_text":   selected_text,
            "keywords":        keywords,
            "matched_domains": matched_domains,
            "saved":           0,
            "articles":        [],
            "message":         "No articles found matching this selection. Try selecting different text.",
        }), 200

    picked = _deduplicate(picked)[:MAX_ARTICLES]

    # ── Step 4: Enrich articles ────────────────────────────────────────────
    enriched: list[dict] = []
    for art in picked:
        try:
            enriched.append(_enrich_article(art))
        except Exception as e:
            log.warning("Enrich failed for %s: %s", art.get("id"), e)
            enriched.append(art)

    # ── Step 5: Tag matched domains per article ────────────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    for art in enriched:
        text_check = (art.get("title", "") + " " + art.get("content", "")).lower()
        art["matched_domains"] = [
            d for d, dkws in DOMAIN_KEYWORDS.items()
            if any(k in text_check for k in dkws)
        ] or matched_domains or ["Technology"]

    # ── Step 6: Save to feed_articles collection ──────────────────────────
    saved: list[dict] = []
    for art in enriched:
        art.setdefault("id", str(uuid.uuid4()))
        art["feed_source"]       = "selection"
        art["selected_text"]     = selected_text
        art["keywords"]          = keywords
        art["matched_domains"]   = art.get("matched_domains", [])
        art["uid"]               = uid
        art["source_article_id"] = source_art_id
        art["fetched_at"]        = now_iso
        art["has_insight"]       = False
        art["table"]             = "feed_articles"   # tag so insight API knows the source

        try:
            db.collection("feed_articles").document(art["id"]).set(art, merge=True)
            saved.append(art)
        except Exception as e:
            log.error("Firestore save failed for feed article %s: %s", art["id"], e)

    log.info(
        "feed/from-selection done: keywords=%s  raw=%d  picked=%d  saved=%d",
        keywords, len(raw_articles), len(picked), len(saved),
    )

    return jsonify({
        "selected_text":   selected_text,
        "keywords":        keywords,
        "matched_domains": matched_domains,
        "saved":           len(saved),
        "articles":        saved,
    }), 201


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/feed/items
# ─────────────────────────────────────────────────────────────────────────────

@feed_bp.route("/items", methods=["GET"])
def list_feed_items():
    """
    GET /api/feed/items?uid=<uid>&limit=20
    List saved feed articles for a user, newest first.

    Query params:
      uid   — filter by user (optional)
      limit — max results (default 20, max 100)

    Response:
    {
      "articles": [
        {
          "id":              "uuid",
          "title":           "...",
          "short_summary":   "...",
          "image_url":       "...",
          "article_url":     "...",
          "matched_domains": ["AI"],
          "source":          "BBC Tech",
          "keywords":        ["machine learning"],
          "selected_text":   "machine learning is...",
          "fetched_at":      "...",
          "has_insight":     false
        }, ...
      ],
      "total": 4
    }

    curl "http://127.0.0.1:5000/api/feed/items?uid=user123"
    curl "http://127.0.0.1:5000/api/feed/items?limit=10"
    """
    uid   = request.args.get("uid", "").strip()
    limit = min(int(request.args.get("limit", 20)), 100)

    try:
        q = db.collection("feed_articles").order_by("fetched_at", direction="DESCENDING")
        if uid:
            q = q.where("uid", "==", uid)
        docs = q.limit(limit).stream()
        articles = [d.to_dict() for d in docs]
    except Exception as e:
        log.error("list_feed_items failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"articles": articles, "total": len(articles)}), 200


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/feed/items/<id>
# ─────────────────────────────────────────────────────────────────────────────

@feed_bp.route("/items/<item_id>", methods=["GET"])
def get_feed_item(item_id: str):
    """
    GET /api/feed/items/<item_id>
    Get a single feed article by ID (includes full_content for Gemini).

    curl http://127.0.0.1:5000/api/feed/items/<item_id>
    """
    doc = db.collection("feed_articles").document(item_id).get()
    if not doc.exists:
        return jsonify({"error": "Feed article not found"}), 404
    return jsonify(doc.to_dict()), 200


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/feed/items/<id>/insight
# ─────────────────────────────────────────────────────────────────────────────

@feed_bp.route("/items/<item_id>/insight", methods=["POST"])
def feed_item_insight(item_id: str):
    """
    POST /api/feed/items/<item_id>/insight
    Generate AI insight for a feed article.
    Same response shape as /api/articles/<id>/insight.

    Pass ?force=1 to skip cache.

    Response:
    {
      "article_id": "uuid",
      "cached":     false,
      "source_table": "feed_articles",
      "ai_insight": {
        "summary":          "150-200 word prose...",
        "svg":              "<svg ...>...</svg>",
        "key_points":       ["...", "...", "..."],
        "sentiment":        "positive",
        "confidence":       0.88,
        "tags":             ["..."],
        "domain":           "AI",
        "reading_time_min": 4,
        "article_url":      "https://..."
      }
    }

    curl -X POST http://127.0.0.1:5000/api/feed/items/<item_id>/insight
    curl -X POST "http://127.0.0.1:5000/api/feed/items/<item_id>/insight?force=1"
    """
    force   = request.args.get("force", "0") in ("1", "true", "yes")
    now_iso = datetime.now(timezone.utc).isoformat()

    doc_ref = db.collection("feed_articles").document(item_id)
    doc     = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "Feed article not found"}), 404

    article = doc.to_dict()

    # ── Cache check ────────────────────────────────────────────────────────
    if not force:
        cached = article.get("ai_insight")
        if (
            cached
            and isinstance(cached, dict)
            and cached.get("summary")
            and not cached.get("raw")
            and len(cached.get("summary", "")) > 50
        ):
            return jsonify({
                "article_id":   item_id,
                "source_table": "feed_articles",
                "ai_insight":   cached,
                "cached":       True,
            }), 200

        insight_doc = db.collection("ai_insights").document(item_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (
                isinstance(stored, dict)
                and stored.get("summary")
                and not stored.get("raw")
                and len(stored.get("summary", "")) > 50
            ):
                return jsonify({
                    "article_id":   item_id,
                    "source_table": "feed_articles",
                    "ai_insight":   stored,
                    "cached":       True,
                }), 200

    # ── Generate ───────────────────────────────────────────────────────────
    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        log.error("Gemini error for feed article %s: %s", item_id, e)
        return jsonify({"error": str(e)}), 502

    # ── Persist on feed article doc ────────────────────────────────────────
    try:
        doc_ref.update({
            "ai_insight":           insight,
            "has_insight":          True,
            "insight_generated_at": now_iso,
        })
    except Exception as e:
        log.error("Failed to update feed article: %s", e)

    # ── Persist to shared ai_insights collection (tagged with source) ──────
    try:
        db.collection("ai_insights").document(item_id).set({
            "article_id":   item_id,
            "source_table": "feed_articles",
            "title":        article.get("title", ""),
            "topic":        (article.get("matched_domains") or ["Technology"])[0],
            "source":       article.get("source", ""),
            "matched_domains": article.get("matched_domains", []),
            "article_url":  article.get("article_url") or article.get("url", ""),
            "keywords":     article.get("keywords", []),
            "selected_text":article.get("selected_text", ""),
            "ai_insight":   insight,
            "created_at":   now_iso,
        })
    except Exception as e:
        log.error("Failed to save ai_insights doc for feed article: %s", e)

    log.info(
        "Feed insight generated: %s  domain=%s  summary_words=%d",
        item_id, insight.get("domain", "?"), len(insight.get("summary", "").split()),
    )
    return jsonify({
        "article_id":   item_id,
        "source_table": "feed_articles",
        "ai_insight":   insight,
        "cached":       False,
    }), 200


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/feed/items/<id>
# ─────────────────────────────────────────────────────────────────────────────

@feed_bp.route("/items/<item_id>", methods=["DELETE"])
def delete_feed_item(item_id: str):
    """
    DELETE /api/feed/items/<item_id>
    Remove a feed article and its cached insight.

    Response:
    { "deleted": true, "article_id": "uuid" }

    curl -X DELETE http://127.0.0.1:5000/api/feed/items/<item_id>
    """
    doc_ref = db.collection("feed_articles").document(item_id)
    if not doc_ref.get().exists:
        return jsonify({"error": "Feed article not found"}), 404

    try:
        doc_ref.delete()
        db.collection("ai_insights").document(item_id).delete()
    except Exception as e:
        log.error("delete_feed_item failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({"deleted": True, "article_id": item_id}), 200