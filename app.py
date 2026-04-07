"""
STEAMI Flask API  v3
— No service account JSON needed (Firestore REST API via web key)
— No NewsAPI needed (RSS-based article fetching)
— Gemini 2.5 Flash for AI insights with SVG
"""

import os
import uuid
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify
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
from static_content import content_bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
app.register_blueprint(content_bp)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": _now()}), 200


# ─────────────────────────────────────────────────────────────────────────────
# SOURCES — expose the RSS source registry to the frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/sources", methods=["GET"])
def list_sources():
    """
    Returns the available RSS sources with their keyword lists.
    Frontend can use this to build the source picker UI.

    curl http://localhost:5000/api/sources
    """
    return jsonify({"sources": get_rss_sources()}), 200


# ─────────────────────────────────────────────────────────────────────────────
# ARTICLES — fetch from RSS & save to Firestore
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/articles/fetch", methods=["POST"])
def fetch_and_save():
    """
    Fetch articles from RSS feeds and save to Firestore.

    Body (JSON):
        topic    : str         — free-text topic / keyword filter
        keywords : list[str]   — explicit keyword list (alternative to topic)
        limit    : int         — max articles to return (default 20)

    curl example:
        curl -X POST http://localhost:5000/api/articles/fetch \\
             -H "Content-Type: application/json" \\
             -d '{"topic": "AI", "limit": 10}'

    curl with keywords (mirrors Next.js AIInsights page):
        curl -X POST http://localhost:5000/api/articles/fetch \\
             -H "Content-Type: application/json" \\
             -d '{"keywords": ["AI", "Robotics"], "limit": 10}'
    """
    data     = request.get_json(silent=True) or {}
    topic    = data.get("topic", "technology")
    keywords = data.get("keywords", [])
    limit    = int(data.get("limit", 20))

    try:
        raw = fetch_articles_from_source(
            topic=topic,
            keywords=keywords,
            limit=limit,
        )
    except Exception as e:
        log.error("fetch error: %s", e)
        return jsonify({"error": str(e)}), 502

    saved = []
    for art in raw:
        art.setdefault("id", str(uuid.uuid4()))
        art["topic"]      = topic
        art["fetched_at"] = _now()
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
    """
    Fetch articles from a user-supplied URL (X, LinkedIn, Facebook, RSS, news site).

    Body (JSON):
        url   : str  — the page/profile/feed URL
        limit : int  — max articles (default 20)

    curl example:
        curl -X POST http://localhost:5000/api/articles/fetch-source \\
             -H "Content-Type: application/json" \\
             -d '{"url": "https://x.com/openai", "limit": 10}'
    """
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

    log.info("fetch-source: saved %d articles from %s", len(saved), url)
    return jsonify({"saved": len(saved), "articles": saved, "source_url": url}), 201


# ─────────────────────────────────────────────────────────────────────────────
# ARTICLES — CRUD
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/articles", methods=["GET"])
def list_articles():
    """
    List articles from Firestore, newest first.

    Query params:
        limit : int (default 30)

    curl http://localhost:5000/api/articles?limit=10
    """
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
    """
    curl http://localhost:5000/api/articles/<article_id>
    """
    doc = db.collection("articles").document(article_id).get()
    if not doc.exists:
        return jsonify({"error": "Not found"}), 404
    return jsonify(doc.to_dict()), 200


@app.route("/api/articles", methods=["POST"])
def create_article():
    """
    Manually create an article.

    curl -X POST http://localhost:5000/api/articles \\
         -H "Content-Type: application/json" \\
         -d '{"title": "Test Article", "content": "Body text here...", "topic": "AI"}'
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# AI INSIGHTS
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/articles/<article_id>/insight", methods=["POST"])
def generate_insight(article_id):
    """
    Generate AI insight (summary + SVG) for an article via Gemini.
    Returns cached result if insight already exists.

    curl -X POST http://localhost:5000/api/articles/<article_id>/insight
    """
    doc_ref = db.collection("articles").document(article_id)
    doc     = doc_ref.get()

    if not doc.exists:
        return jsonify({"error": "Article not found"}), 404

    article = doc.to_dict()

    # Check article-level cache
    if article.get("ai_insight"):
        log.info("Returning cached insight for %s", article_id)
        return jsonify({
            "article_id": article_id,
            "ai_insight": article["ai_insight"],
            "cached":     True,
        }), 200

    # Check dedicated insights collection
    insight_doc = db.collection("ai_insights").document(article_id).get()
    if insight_doc.exists:
        insight_data = insight_doc.to_dict()
        log.info("Returning insight from ai_insights collection for %s", article_id)
        return jsonify({
            "article_id": article_id,
            "ai_insight": insight_data.get("ai_insight", insight_data),
            "cached":     True,
        }), 200

    # Generate fresh insight
    try:
        insight = generate_ai_insight(article)
    except Exception as e:
        log.error("Gemini error for %s: %s", article_id, e)
        return jsonify({"error": str(e)}), 502

    # Save back to article
    try:
        doc_ref.update({
            "ai_insight":           insight,
            "has_insight":          True,
            "insight_generated_at": _now(),
        })
    except Exception as e:
        log.error("Failed to update article with insight: %s", e)

    # Save to dedicated insights collection
    try:
        db.collection("ai_insights").document(article_id).set({
            "article_id": article_id,
            "title":      article.get("title", ""),
            "topic":      article.get("topic", ""),
            "source":     article.get("source", ""),
            "ai_insight": insight,
            "created_at": _now(),
        })
    except Exception as e:
        log.error("Failed to save ai_insights doc: %s", e)

    log.info("Insight generated and saved for %s", article_id)
    return jsonify({
        "article_id": article_id,
        "ai_insight": insight,
        "cached":     False,
    }), 200


@app.route("/api/insights", methods=["GET"])
def list_insights():
    """
    curl http://localhost:5000/api/insights
    """
    try:
        docs = (
            db.collection("ai_insights")
              .order_by("created_at", direction="DESCENDING")
              .limit(50)
              .stream()
        )
        return jsonify({"insights": [d.to_dict() for d in docs]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/insights/<article_id>", methods=["GET"])
def get_insight(article_id):
    """
    curl http://localhost:5000/api/insights/<article_id>
    """
    doc = db.collection("ai_insights").document(article_id).get()
    if not doc.exists:
        return jsonify({"error": "Insight not found"}), 404
    return jsonify(doc.to_dict()), 200


# ─────────────────────────────────────────────────────────────────────────────
# FULL PIPELINE (fetch + insight in one shot)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/pipeline", methods=["POST"])
def pipeline():
    """
    Fetch articles via RSS and immediately generate AI insights for all of them.

    curl -X POST http://localhost:5000/api/pipeline \\
         -H "Content-Type: application/json" \\
         -d '{"keywords": ["AI", "Machine Learning"], "limit": 2}'
    """
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
            results.append({"id": art["id"], "title": art.get("title", ""),
                            "ai_insight": insight, "status": "ok"})
        except Exception as e:
            results.append({"id": art["id"], "status": "error", "error": str(e)})

    return jsonify({"processed": len(results), "results": results}), 201


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Starting STEAMI Flask API on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)