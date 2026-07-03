"""
insight_validation.py — Bad-News Detection Validation & Transparency
======================================================================
Backs two things from the STEAMI gap-analysis report, item 3
("Bad News Detection Validation"):

  1. A public "How we classify" methodology page — what sentiment, risk,
     and confidence actually mean and how they're derived. See
     ollama_agent.py for the live prompt/weights this describes; keep
     METHODOLOGY below in sync if you change either.

  2. A public "model validation metrics" readout. This is an LLM prompt
     classifier, not a trained/fine-tuned model, so there is no real
     precision/recall to publish. The honest metric we CAN publish is
     mod/admin agreement: mods spot-review individual classifications as
     "confirmed" or "incorrect", and we aggregate that into an agreement
     rate — overall and broken down by sentiment_label, so "how good is
     bad-news detection specifically" has a real, visible answer.

ENDPOINTS (mounted at /api/classification — see note below):
  GET  /api/classification/methodology           — PUBLIC — static methodology description
  POST /api/classification/{article_id}/review    — MOD/ADMIN — record a verdict
  GET  /api/classification/{article_id}/review    — PUBLIC — this item's review, if any
  GET  /api/classification/validation-stats       — PUBLIC — aggregate accuracy

NOTE ON PREFIX: this router is intentionally mounted at /api/classification,
NOT /api/insights, even though it's about AI insight classification. The
existing insight_router already owns GET /api/insights/{article_id} — mounting
here too would let /api/insights/methodology and /api/insights/validation-stats
get swallowed by that {article_id} pattern (both are single path segments that
match it). Keep this on its own prefix.

INTEGRATION (add to main.py):
  ① Import at the top:
        from insight_validation import router as insight_validation_router

  ② Register alongside your other routers:
        app.include_router(insight_validation_router, prefix="/api/classification", tags=["Insight Validation"])

Firestore collection: classification_reviews
  { article_id, source_table, title, sentiment_label, risk_level,
    confidence, verdict, note, reviewer_uid, reviewed_at }
  One document per article_id — re-reviewing overwrites the previous verdict.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from mongodb_client import db
from auth import require_mod, get_uid

log = logging.getLogger(__name__)
router = APIRouter()


def _now() -> str:
    """Current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────

class ReviewBody(BaseModel):
    verdict:      str            # "confirmed" | "incorrect"
    note:         Optional[str] = None
    source_table: str           = "articles"   # "articles" | "feed_articles"


# ─────────────────────────────────────────────────────────────────────────────
# METHODOLOGY — static, versioned description of how classification works.
# Keep this in sync with ollama_agent.py's INSIGHT_PROMPT / CONFIDENCE_WEIGHTS
# / _DEFAULT_RISK_BY_SENTIMENT if either changes.
# ─────────────────────────────────────────────────────────────────────────────

METHODOLOGY = {
    "version":         "1.0",
    "classifier_type": "LLM prompt classification (not a trained/fine-tuned model)",
    "model_note":      "Ollama Cloud — see OLLAMA_API_KEY / model config in ollama_agent.py",
    "sentiment": {
        "categories":    ["positive", "neutral", "negative"],
        "mapped_labels": {"positive": "good_news", "neutral": "neutral_news", "negative": "bad_news"},
        "sentiment_score_range": [-1.0, 1.0],
        "description": (
            "Each article is read in isolation and classified into one of three "
            "categories, plus a continuous sentiment_score capturing magnitude, "
            "based on the article's own reported content — not on external "
            "fact-checking or cross-referencing other sources."
        ),
    },
    "risk_level": {
        "categories": ["low", "medium", "high"],
        "description": (
            "risk_level estimates real-world severity/scope independent of the "
            "sentiment category — e.g. a 'negative' article about a minor delay "
            "is low risk, while one about a safety recall is high risk. Always "
            "accompanied by a one-line rationale explaining the classification."
        ),
    },
    "confidence": {
        "weights": {"source_clarity": 0.35, "claim_specificity": 0.35, "domain_consensus": 0.30},
        "description": (
            "confidence is not a single self-reported number. It is a weighted "
            "average of three named factors the model rates for each article: "
            "source_clarity (35%), claim_specificity (35%), and domain_consensus "
            "(30%). The full breakdown is visible on every individual insight via "
            "the \"Why this score?\" explainer."
        ),
    },
    "known_limitations": [
        "This is a single LLM pass per article, not an ensemble or fine-tuned classifier.",
        "No formal train/test split exists because this isn't a trained model — there is no precision/recall in the traditional ML sense.",
        "The accuracy figures below come from mod/admin spot-review, not exhaustive labeling — check total_reviewed for the current sample size before drawing conclusions.",
        "Classification is per-article only; it does not fact-check claims against external sources.",
    ],
}


@router.get(
    "/methodology",
    tags=["Insight Validation"],
    summary="How sentiment/risk/confidence are classified — PUBLIC",
)
def get_methodology():
    """GET /api/classification/methodology — public, static methodology description."""
    return METHODOLOGY


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/classification/{article_id}/review — MOD/ADMIN
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/{article_id}/review",
    tags=["Insight Validation"],
    summary="Confirm or flag an insight's classification — MOD/ADMIN",
)
def review_insight(article_id: str, body: ReviewBody, payload: dict = Depends(require_mod)):
    """
    POST /api/classification/{article_id}/review — MOD/ADMIN

    Records a human verdict on whether the AI's sentiment/risk classification
    for this article was correct. One review per article — re-submitting
    overwrites the previous verdict.

    Body: { "verdict": "confirmed" | "incorrect", "note": "optional reason", "source_table": "articles" }
    """
    if body.verdict not in ("confirmed", "incorrect"):
        raise HTTPException(400, detail="verdict must be 'confirmed' or 'incorrect'")

    table = body.source_table if body.source_table in ("articles", "feed_articles") else "articles"

    try:
        art_doc = db.collection(table).document(article_id).get()
        if not art_doc.exists:
            raise HTTPException(404, detail=f"{article_id} not found in {table}")
        article = art_doc.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"DB read failed: {e}")

    ai = article.get("ai_insight") or {}

    doc = {
        "article_id":      article_id,
        "source_table":    table,
        "title":           article.get("title", ""),
        "sentiment_label": ai.get("sentiment_label", "neutral_news"),
        "risk_level":      ai.get("risk_level"),
        "confidence":      ai.get("confidence"),
        "verdict":         body.verdict,
        "note":            (body.note or "")[:500],
        "reviewer_uid":    get_uid(payload),
        "reviewed_at":     _now(),
    }

    try:
        db.collection("classification_reviews").document(article_id).set(doc, merge=True)
    except Exception as e:
        log.error("review_insight: save failed for %s: %s", article_id, e)
        raise HTTPException(500, detail=f"Save failed: {e}")

    log.info("review_insight: %s marked %s by %s", article_id, body.verdict, doc["reviewer_uid"])
    return {"saved": True, "review": doc}


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/classification/{article_id}/review — PUBLIC
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{article_id}/review",
    tags=["Insight Validation"],
    summary="Get this article's review, if any — PUBLIC",
)
def get_insight_review(article_id: str):
    """GET /api/classification/{article_id}/review — public. Returns null if unreviewed."""
    try:
        doc = db.collection("classification_reviews").document(article_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as e:
        raise HTTPException(500, detail=f"DB read failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/classification/validation-stats — PUBLIC
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/validation-stats",
    tags=["Insight Validation"],
    summary="Aggregate classification accuracy — PUBLIC",
)
def validation_stats():
    """
    GET /api/classification/validation-stats — public

    Aggregates every mod-reviewed classification into overall + per-label
    agreement rates. This is the "model validation metrics" surface — honest
    about sample size since it's built from spot-review, not a formal
    labeled test set.

    Response shape:
      {
        "total_reviewed": 42, "confirmed": 39, "incorrect": 3, "agreement_pct": 92.9,
        "by_label": {
          "bad_news":     {"reviewed": 12, "confirmed": 11, "incorrect": 1, "agreement_pct": 91.7},
          "good_news":    {...}, "neutral_news": {...}
        },
        "last_reviewed_at": "2026-07-02T12:00:00+00:00",
        "sample_size_note": "..."
      }
    """
    try:
        docs = db.collection("classification_reviews").stream_all()
    except Exception as e:
        raise HTTPException(500, detail=f"DB read failed: {e}")

    by_label: dict = {}
    total = confirmed = incorrect = 0
    last_reviewed_at = None

    for doc in docs:
        d = doc.to_dict() if hasattr(doc, "to_dict") else doc
        label   = d.get("sentiment_label") or "neutral_news"
        verdict = d.get("verdict")

        bucket = by_label.setdefault(label, {"reviewed": 0, "confirmed": 0, "incorrect": 0})
        bucket["reviewed"] += 1
        total += 1
        if verdict == "confirmed":
            bucket["confirmed"] += 1
            confirmed += 1
        elif verdict == "incorrect":
            bucket["incorrect"] += 1
            incorrect += 1

        ts = d.get("reviewed_at")
        if ts and (last_reviewed_at is None or ts > last_reviewed_at):
            last_reviewed_at = ts

    for bucket in by_label.values():
        bucket["agreement_pct"] = (
            round(100 * bucket["confirmed"] / bucket["reviewed"], 1) if bucket["reviewed"] else None
        )

    return {
        "total_reviewed":   total,
        "confirmed":        confirmed,
        "incorrect":        incorrect,
        "agreement_pct":    round(100 * confirmed / total, 1) if total else None,
        "by_label":         by_label,
        "last_reviewed_at": last_reviewed_at,
        "sample_size_note": (
            "Based on mod/admin spot-review, not exhaustive labeling. "
            "See total_reviewed for the current sample size."
        ),
    }
