"""
STEAMI Flask API  v4
— All blueprints registered: chat, feed, static_content
— CORS handles OPTIONS preflight for every /api/* route
"""

import os
import uuid
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from firestore_client import db
from gemini_client import generate_ai_insight
from article_fetcher import (
    fetch_articles_from_source,
    fetch_articles_from_url,
    get_rss_sources,
)
from chat import chat_bp
from feed import feed_bp
from static_content import content_bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# CORS must be configured BEFORE blueprints are registered so the
# after_request CORS headers apply to every route including blueprint routes.
CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    supports_credentials=False,
)

# ── Register blueprints ────────────────────────────────────────────────────
app.register_blueprint(chat_bp)     # mounts at /api/chat/...
app.register_blueprint(feed_bp)     # mounts at /api/feed/...
app.register_blueprint(content_bp)  # mounts at /api/explainers/... and /api/research/...


# ── Belt-and-suspenders OPTIONS handler ───────────────────────────────────
# Flask-CORS sometimes misses blueprint preflight requests when the route
# returns 404. This before_request hook catches every OPTIONS call first
# and immediately returns 200 with the correct CORS headers.
@app.before_request
def handle_options_preflight():
    if request.method == "OPTIONS":
        resp = make_response("", 200)
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization"
        )
        resp.headers["Access-Control-Max-Age"] = "3600"
        return resp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": _now()}), 200


# ══════════════════════════════════════════════════════════════════════════
# SOURCES
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/sources", methods=["GET"])
def list_sources():
    return jsonify({"sources": get_rss_sources()}), 200


# ══════════════════════════════════════════════════════════════════════════
# ARTICLES — fetch from RSS & save
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/articles/fetch", methods=["POST"])
def fetch_and_save():
    data     = request.get_json(silent=True) or {}
    topic    = data.get("topic", "technology")
    keywords = data.get("keywords", [])
    limit    = int(data.get("limit", 20))

    try:
        raw = fetch_articles_from_source(topic=topic, keywords=keywords, limit=limit)
    except Exception as e:
        log.error("fetch error: %s", e)
        return jsonify({"error": str(e)}), 502

    saved = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art["topic"]       = topic
        art["fetched_at"]  = _now()
        art["has_insight"] = False
        try:
            db.collection("articles").document(art["id"]).set(art, merge=True)
            saved.append(art)
        except Exception as e:
            log.error("Firestore save failed for %s: %s", art["id"], e)

    log.info("Fetched %d articles, saved %d", len(raw), len(saved))
    return jsonify({"saved": len(saved), "articles": saved}), 201


@app.route("/api/articles/fetch-source", methods=["POST"])
def fetch_from_source_url():
    data  = request.get_json(silent=True) or {}
    url   = (data.get("url") or "").strip()
    limit = int(data.get("limit", 20))

    if not url:
        return jsonify({"error": "url is required"}), 400

    try:
        raw = fetch_articles_from_url(url=url, limit=limit)
    except Exception as e:
        log.error("fetch-source error: %s", e)
        return jsonify({"error": str(e)}), 502

    saved = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art["source_url"]  = url
        art["fetched_at"]  = _now()
        art["has_insight"] = False
        try:
            db.collection("articles").document(art["id"]).set(art, merge=True)
            saved.append(art)
        except Exception as e:
            log.error("Firestore save failed: %s", e)

    log.info("fetch-source: saved %d from %s", len(saved), url)
    return jsonify({"saved": len(saved), "articles": saved, "source_url": url}), 201


# ══════════════════════════════════════════════════════════════════════════
# ARTICLES — CRUD
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/articles", methods=["GET"])
def list_articles():
    limit = int(request.args.get("limit", 30))
    try:
        docs = (
            db.collection("articles")
              .order_by("fetched_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        return jsonify({"articles": [d.to_dict() for d in docs]}), 200
    except Exception as e:
        log.error("list_articles error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/articles/<article_id>", methods=["GET"])
def get_article(article_id):
    doc = db.collection("articles").document(article_id).get()
    if not doc.exists:
        return jsonify({"error": "Not found"}), 404
    return jsonify(doc.to_dict()), 200


@app.route("/api/articles", methods=["POST"])
def create_article():
    data = request.get_json(silent=True) or {}
    if not data.get("title") or not data.get("content"):
        return jsonify({"error": "title and content are required"}), 400
    doc_id = str(uuid.uuid4())
    art = {
        "id":          doc_id,
        "title":       data["title"],
        "content":     data["content"],
        "url":         data.get("url", ""),
        "source":      data.get("source", "manual"),
        "topic":       data.get("topic", "general"),
        "fetched_at":  _now(),
        "has_insight": False,
    }
    db.collection("articles").document(doc_id).set(art)
    return jsonify(art), 201


# ══════════════════════════════════════════════════════════════════════════
# AI INSIGHTS
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/articles/<article_id>/insight", methods=["DELETE"])
def delete_insight(article_id):
    """Clear cached insight — next POST will regenerate from Gemini."""
    doc_ref = db.collection("articles").document(article_id)
    if not doc_ref.get().exists:
        return jsonify({"error": "Article not found"}), 404
    try:
        doc_ref.update({
            "ai_insight":           None,
            "has_insight":          False,
            "insight_generated_at": None,
        })
    except Exception as e:
        log.warning("Could not clear article insight fields: %s", e)
    try:
        db.collection("ai_insights").document(article_id).delete()
    except Exception as e:
        log.warning("Could not delete ai_insights doc: %s", e)
    log.info("Insight cache cleared for %s", article_id)
    return jsonify({"deleted": True, "article_id": article_id}), 200


@app.route("/api/articles/<article_id>/insight", methods=["POST"])
def generate_insight(article_id):
    """
    Generate Gemini insight for an article.
    Searches both `articles` AND `feed_articles` so no article is missed.
    Pass ?force=1 to skip cache and regenerate.
    """
    force = request.args.get("force", "0") in ("1", "true", "yes")

    # Search both collections
    source_table = "articles"
    doc_ref      = db.collection("articles").document(article_id)
    doc          = doc_ref.get()
    if not doc.exists:
        doc_ref      = db.collection("feed_articles").document(article_id)
        doc          = doc_ref.get()
        source_table = "feed_articles"
    if not doc.exists:
        return jsonify({"error": "Article not found in articles or feed_articles"}), 404

    article = doc.to_dict()

    # Cache check
    if not force:
        cached = article.get("ai_insight")
        if (
            cached
            and isinstance(cached, dict)
            and cached.get("summary")
            and not cached.get("raw")
            and len(cached.get("summary", "")) > 50
        ):
            log.info("Returning cached insight for %s (table=%s)", article_id, source_table)
            return jsonify({
                "article_id":   article_id,
                "source_table": source_table,
                "ai_insight":   cached,
                "cached":       True,
            }), 200

        insight_doc = db.collection("ai_insights").document(article_id).get()
        if insight_doc.exists:
            stored = insight_doc.to_dict().get("ai_insight", {})
            if (
                isinstance(stored, dict)
                and stored.get("summary")
                and not stored.get("raw")
                and len(stored.get("summary", "")) > 50
            ):
                log.info("Returning cached ai_insights for %s", article_id)
                return jsonify({
                    "article_id":   article_id,
                    "source_table": insight_doc.to_dict().get("source_table", source_table),
                    "ai_insight":   stored,
                    "cached":       True,
                }), 200
            else:
                try:
                    db.collection("ai_insights").document(article_id).delete()
                except Exception:
                    pass

    # Generate fresh
    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        log.error("Gemini error for %s: %s", article_id, e)
        return jsonify({"error": str(e)}), 502

    try:
        doc_ref.update({
            "ai_insight":           insight,
            "has_insight":          True,
            "insight_generated_at": _now(),
        })
    except Exception as e:
        log.error("Failed to update %s/%s: %s", source_table, article_id, e)

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
        log.error("Failed to save ai_insights doc: %s", e)

    log.info(
        "Insight generated: %s  table=%s  domain=%s  words=%d  svg=%d chars",
        article_id, source_table,
        insight.get("domain", "?"),
        len(insight.get("summary", "").split()),
        len(str(insight.get("svg", ""))),
    )
    return jsonify({
        "article_id":   article_id,
        "source_table": source_table,
        "ai_insight":   insight,
        "cached":       False,
    }), 200


@app.route("/api/insights", methods=["GET"])
def list_insights():
    limit = int(request.args.get("limit", 50))
    try:
        docs = (
            db.collection("ai_insights")
              .order_by("created_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        return jsonify({"insights": [d.to_dict() for d in docs]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/insights/<article_id>", methods=["GET"])
def get_insight(article_id):
    doc = db.collection("ai_insights").document(article_id).get()
    if not doc.exists:
        return jsonify({"error": "Insight not found"}), 404
    return jsonify(doc.to_dict()), 200


# ══════════════════════════════════════════════════════════════════════════
# PIPELINE  (legacy)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/pipeline", methods=["POST"])
def pipeline():
    data     = request.get_json(silent=True) or {}
    topic    = data.get("topic", "technology")
    keywords = data.get("keywords", [])
    limit    = int(data.get("limit", 3))

    try:
        raw = fetch_articles_from_source(topic=topic, keywords=keywords, limit=limit)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    results = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art.update({"topic": topic, "fetched_at": _now(), "has_insight": False})
        db.collection("articles").document(art["id"]).set(art, merge=True)
        try:
            insight = generate_ai_insight(art)
            db.collection("articles").document(art["id"]).update({
                "ai_insight":           insight,
                "has_insight":          True,
                "insight_generated_at": _now(),
            })
            db.collection("ai_insights").document(art["id"]).set({
                "article_id": art["id"],
                "title":      art.get("title", ""),
                "topic":      topic,
                "ai_insight": insight,
                "created_at": _now(),
            })
            results.append({
                "id": art["id"], "title": art.get("title", ""),
                "ai_insight": insight, "status": "ok",
            })
        except Exception as e:
            results.append({"id": art["id"], "status": "error", "error": str(e)})

    return jsonify({"processed": len(results), "results": results}), 201


# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Starting STEAMI Flask API v4 on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)