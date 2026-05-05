"""
routers/notifications.py
========================
GET /api/notifications/latest
  — Public endpoint. Returns new explainers, research articles, and blog posts
    added after the given `since` ISO-8601 timestamp.

    Query params:
      since   (str)  ISO-8601 datetime string, e.g. "2026-05-01T00:00:00Z"
                     Defaults to 7 days ago if omitted.
      limit   (int)  Max items per category. Default 10, max 50.

    Response:
      {
        "total": 5,
        "since": "2026-05-01T00:00:00+00:00",
        "items": [
          {
            "id":         "quantum-dog",
            "type":       "explainer",        // "explainer" | "research" | "blog"
            "title":      "The Quantum Dog",
            "field":      "PHYSICS",
            "image":      "/images/explainers/quantum-dog.jpg",
            "created_at": "2026-05-03T12:00:00+00:00",
            "url":        "/explainers/quantum-dog"
          },
          ...
        ]
      }

Usage in main.py:
  from routers.notifications import router as notifications_router
  app.include_router(notifications_router, prefix="/api/notifications", tags=["Notifications"])
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

# ── Import the shared Firestore client from your existing db module ──────────
# Adjust this import to match how you expose `db` in your project.
# Common patterns:
#   from db import db
#   from main import db
#   from firebase_admin import firestore; db = firestore.client()
try:
    from db import db  # preferred — a dedicated db.py that exports the client
except ImportError:
    from main import db  # fallback if db is defined in main.py

router = APIRouter()


def _parse_since(since: Optional[str], default_days: int = 7) -> datetime:
    """Parse the `since` query param into a UTC-aware datetime."""
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid `since` value: {since!r}. "
                       "Use ISO-8601 format, e.g. '2026-05-01T00:00:00Z'.",
            )
    return datetime.now(timezone.utc) - timedelta(days=default_days)


def _fetch_new(
    collection: str,
    type_label: str,
    url_prefix: str,
    since_iso: str,
    limit: int,
) -> list[dict]:
    """
    Query a Firestore collection for documents with created_at >= since_iso.
    Returns a normalised list ready for the notification response.
    """
    try:
        docs = (
            db.collection(collection)
              .order_by("created_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
    except Exception as exc:
        # Log but don't crash the whole endpoint if one collection fails
        import logging
        logging.getLogger(__name__).warning(
            "notifications: error querying %s — %s", collection, exc
        )
        return []

    results = []
    for doc in docs:
        data = doc.to_dict()
        created = data.get("created_at", "")
        if not created or created < since_iso:
            # Documents are ordered DESC so once we're before the cutoff, stop
            break

        doc_id = data.get("id") or doc.id
        results.append({
            "id":         doc_id,
            "type":       type_label,
            "title":      data.get("title", "Untitled"),
            "field":      data.get("field", ""),
            "image":      data.get("image") or data.get("cover_image") or data.get("coverImage") or "",
            "created_at": created,
            "url":        f"{url_prefix}/{doc_id}",
        })

    return results


@router.get(
    "/latest",
    summary="Get latest content notifications — public",
    description="""
GET /api/notifications/latest?since=2026-05-01T00:00:00Z&limit=10

Returns new Explainers, Research Articles, and Blog Posts added after
the given `since` timestamp. No authentication required.

- `since`  ISO-8601 string (defaults to 7 days ago)
- `limit`  max items per category, 1–50 (default 10)

The frontend should store the timestamp of the last successful poll
(e.g. in localStorage as `steami_notif_since`) and pass it back on
every subsequent call to avoid re-showing already-seen notifications.
""",
    tags=["Notifications"],
)
def get_latest_notifications(
    since: Optional[str] = Query(
        None,
        description="ISO-8601 datetime. Only items created after this are returned.",
        example="2026-05-01T00:00:00Z",
    ),
    limit: int = Query(10, ge=1, le=50, description="Max results per content type."),
):
    since_dt  = _parse_since(since)
    since_iso = since_dt.isoformat()

    explainers = _fetch_new("explainers",        "explainer", "/explainers",       since_iso, limit)
    research   = _fetch_new("research_articles", "research",  "/research",         since_iso, limit)
    blog       = _fetch_new("blog_posts",        "blog",      "/blog",             since_iso, limit)

    # Merge and sort newest-first
    all_items = sorted(
        explainers + research + blog,
        key=lambda x: x["created_at"],
        reverse=True,
    )

    return {
        "total": len(all_items),
        "since": since_iso,
        "items": all_items,
    }