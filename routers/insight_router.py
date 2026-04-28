"""
routers/insight_router.py  —  On-Demand Article Insight Generation  v1
=======================================================================
Provides a single endpoint for mods and admins to trigger AI insight
generation for a specific article by its ID.

ACCESS CONTROL:
  POST /api/articles/{article_id}/generate-insight  — mod | admin only

BEHAVIOUR:
  1. Fetches the article from the `articles` collection.
  2. Checks whether an insight already exists (has_insight == True).
     - If yes:  returns the cached insight unless ?force=true is passed.
     - If no:   generates a fresh insight via generate_ai_insight().
  3. Writes the insight back to:
       - articles/{article_id}      (inline fields: ai_insight, has_insight, insight_generated_at)
       - ai_insights/{article_id}   (dedicated insights collection — mirrors main.py behaviour)
  4. Returns the full insight payload plus metadata.

QUERY PARAMS:
  force (bool, default False) — re-generate even if insight already exists.

ERRORS:
  401  — no/invalid token
  403  — authenticated but role is "user"
  404  — article not found
  409  — insight already exists (only when force=False and insight present)
  500  — DB error or AI generation failure

INTEGRATION (add to main.py):
  from routers.insight_router import router as insight_router
  app.include_router(insight_router, prefix="/api/articles", tags=["Insights"])
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from mongodb_client import db
from auth import require_mod, get_uid
from ollama_agent import generate_ai_insight

log    = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _get_article(article_id: str) -> dict:
    """
    Fetch a single article document from Firestore.
    Raises HTTP 404 if not found, HTTP 500 on DB errors.
    """
    try:
        doc = db.collection("articles").document(article_id).get()
    except Exception as exc:
        log.error("insight_router: DB read failed for article=%s: %s", article_id, exc)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if not doc.exists:
        raise HTTPException(
            status_code=404,
            detail=f"Article '{article_id}' not found.",
        )
    return doc.to_dict()


def _save_insight(article_id: str, article: dict, insight: dict) -> None:
    """
    Persist the generated insight to both Firestore collections:
      - articles/{article_id}      — inline ai_insight fields
      - ai_insights/{article_id}   — dedicated insights collection
    Raises HTTP 500 if either write fails.
    """
    generated_at = _now()

    # 1. Update the article document in-place
    try:
        db.collection("articles").document(article_id).update({
            "ai_insight":            insight,
            "has_insight":           True,
            "insight_generated_at":  generated_at,
        })
    except Exception as exc:
        log.error("insight_router: failed to update article=%s: %s", article_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to save insight to article: {exc}")

    # 2. Upsert into the dedicated ai_insights collection
    try:
        db.collection("ai_insights").document(article_id).set({
            "article_id":      article_id,
            "source_table":    "articles",
            "title":           article.get("title", ""),
            "topic":           article.get("topic", ""),
            "source":          article.get("source", ""),
            "matched_domains": article.get("matched_domains", []),
            "article_url":     article.get("article_url") or article.get("url", ""),
            "ai_insight":      insight,
            "created_at":      generated_at,
        })
    except Exception as exc:
        log.error("insight_router: failed to write ai_insights/%s: %s", article_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to save insight record: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/{article_id}/generate-insight",
    summary="Generate AI insight for an article — MOD / ADMIN only",
    response_description=(
        "The generated (or cached) AI insight together with article metadata."
    ),
)
def generate_insight_for_article(
    article_id: str,
    force:      bool = Query(
        default=False,
        description=(
            "Set to true to regenerate the insight even if one already exists. "
            "Defaults to false — returns cached insight when available."
        ),
    ),
    payload:    dict = Depends(require_mod),   # enforces mod | admin
):
    """
    **POST /api/articles/{article_id}/generate-insight**

    Triggers on-demand AI insight generation for the specified article.

    - Requires **mod** or **admin** role (HTTP 403 for plain users).
    - Returns the **cached insight** when `has_insight` is already `true`,
      unless `?force=true` is supplied to force a re-generation.
    - Writes the result back to both `articles` and `ai_insights` collections,
      mirroring the behaviour of the background insight queue in `main.py`.

    **Example request:**
    ```
    POST /api/articles/abc123/generate-insight
    Authorization: Bearer <mod_or_admin_token>
    ```

    **Force re-generation:**
    ```
    POST /api/articles/abc123/generate-insight?force=true
    Authorization: Bearer <admin_token>
    ```
    """
    caller_uid  = get_uid(payload)
    caller_role = payload.get("role", "unknown")

    log.info(
        "insight_router: generate requested — article=%s force=%s caller=%s role=%s",
        article_id, force, caller_uid, caller_role,
    )

    # ── 1. Load article ───────────────────────────────────────────────────────
    article = _get_article(article_id)

    # ── 2. Return cached insight if already generated and force=False ─────────
    if article.get("has_insight") and not force:
        cached = article.get("ai_insight", {})
        log.info(
            "insight_router: returning cached insight for article=%s", article_id
        )
        return {
            "article_id":           article_id,
            "title":                article.get("title", ""),
            "cached":               True,
            "force_regenerated":    False,
            "insight_generated_at": article.get("insight_generated_at"),
            "insight":              cached,
            "message": (
                "Insight already exists. Pass ?force=true to regenerate."
            ),
        }

    # ── 3. Generate insight via Ollama ────────────────────────────────────────
    log.info(
        "insight_router: calling generate_ai_insight for article=%s (force=%s)",
        article_id, force,
    )
    try:
        insight = generate_ai_insight(article)
    except (RuntimeError, ValueError) as exc:
        log.error(
            "insight_router: generate_ai_insight failed for article=%s: %s",
            article_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"AI insight generation failed: {exc}",
        )
    except Exception as exc:
        log.exception(
            "insight_router: unexpected error generating insight for article=%s",
            article_id,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error during insight generation: {exc}",
        )

    # ── 4. Persist to Firestore ───────────────────────────────────────────────
    _save_insight(article_id, article, insight)

    was_regenerated = bool(article.get("has_insight"))
    log.info(
        "insight_router: insight %s for article=%s by %s (%s)",
        "regenerated" if was_regenerated else "generated",
        article_id, caller_uid, caller_role,
    )

    return {
        "article_id":           article_id,
        "title":                article.get("title", ""),
        "cached":               False,
        "force_regenerated":    was_regenerated and force,
        "insight_generated_at": _now(),
        "insight":              insight,
        "message": (
            "Insight regenerated successfully."
            if was_regenerated else
            "Insight generated successfully."
        ),
    }