"""
routers/dashboard.py  —  Activity Dashboard API
=================================================
Tracks every time a user opens a popup in the STEAMI app.
This data powers the admin dashboard and the user's own activity view.

HOW IT WORKS:
  Frontend calls POST /api/dashboard/event every time a popup is opened.
  The event records: who, what type of popup, which item, when.
  The dashboard endpoint aggregates this into useful stats.

POPUP TYPES tracked:
  research_article | ai_insight | explainer | simulation

ENDPOINTS:
  POST /api/dashboard/event              — log a popup open (requires auth)
  GET  /api/dashboard/me                 — own activity summary (requires auth)
  GET  /api/dashboard/admin              — platform-wide stats (admin only)
  GET  /api/dashboard/admin/events       — raw event log (admin only)

Firestore collection: `popup_events`
Fields: id, uid, popup_type, popup_id, popup_title, opened_at (ISO string),
        date (YYYY-MM-DD for easy grouping), hour (0-23 for heat map)
"""

import logging
import uuid
from datetime import datetime, timezone
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from firestore_client import db
from auth import require_auth, require_admin, get_uid

log    = logging.getLogger(__name__)
router = APIRouter()

# Valid popup types — must match diary.py VALID_POPUP_TYPES
VALID_POPUP_TYPES: list[str] = [
    "research_article",
    "ai_insight",
    "explainer",
    "simulation",
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    """Return today's date as YYYY-MM-DD (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _hour_now() -> int:
    """Return current UTC hour (0–23) for heat-map grouping."""
    return datetime.now(timezone.utc).hour


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class PopupEventBody(BaseModel):
    """
    Sent by the frontend every time a popup is opened.
    popup_title is optional but helps the dashboard display nicely.
    """
    popup_type:  str           # research_article | ai_insight | explainer | simulation
    popup_id:    str           # ID of the item being opened
    popup_title: str = ""      # optional display title (article title, explainer title, etc.)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/event",
    status_code = 201,
    summary     = "Log popup open event — requires auth",
)
def log_popup_event(
    body:    PopupEventBody,
    payload: dict = Depends(require_auth),  # any logged-in user
):
    """
    POST /api/dashboard/event
    Called by the frontend every time a user opens any popup.
    Records who opened it, which item, and when.

    Body:
    {
      "popup_type":  "explainer",
      "popup_id":    "quantum-dog",
      "popup_title": "The Quantum Dog: Schrödinger's Pet Paradox"
    }

    Response:
    {
      "id":          "event-uuid",
      "uid":         "user-uuid",
      "popup_type":  "explainer",
      "popup_id":    "quantum-dog",
      "popup_title": "The Quantum Dog...",
      "opened_at":   "2026-04-09T14:32:00+00:00",
      "date":        "2026-04-09",
      "hour":        14
    }

    curl -X POST http://127.0.0.1:5000/api/dashboard/event \\
      -H "Authorization: Bearer <token>" \\
      -H "Content-Type: application/json" \\
      -d '{"popup_type":"explainer","popup_id":"quantum-dog","popup_title":"Quantum Dog"}'
    """
    # Validate popup type
    if body.popup_type not in VALID_POPUP_TYPES:
        raise HTTPException(
            400,
            detail=f"Invalid popup_type. Must be one of: {', '.join(VALID_POPUP_TYPES)}"
        )

    if not body.popup_id.strip():
        raise HTTPException(400, detail="popup_id is required.")

    uid      = get_uid(payload)
    event_id = str(uuid.uuid4())
    now      = datetime.now(timezone.utc)

    event = {
        "id":          event_id,
        "uid":         uid,
        "popup_type":  body.popup_type,
        "popup_id":    body.popup_id.strip(),
        "popup_title": body.popup_title.strip(),
        "opened_at":   now.isoformat(),           # full ISO timestamp
        "date":        now.strftime("%Y-%m-%d"),   # YYYY-MM-DD — for daily grouping
        "hour":        now.hour,                   # 0–23 — for hourly heat map
    }

    try:
        db.collection("popup_events").document(event_id).set(event)
    except Exception as e:
        log.error("log_popup_event failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    log.info("popup event: uid=%s type=%s id=%s", uid, body.popup_type, body.popup_id)
    return event


@router.get(
    "/me",
    summary = "Own activity summary — requires auth",
)
def my_dashboard(
    limit:   int = Query(100, ge=1, le=500),
    payload: dict = Depends(require_auth),
):
    """
    GET /api/dashboard/me
    Returns the current user's popup activity summary:
    - recent events list (with date and time)
    - counts per popup type
    - counts per day (for a simple calendar/streak view)
    - most opened items

    Response:
    {
      "total_events": 42,
      "by_type": {
        "explainer": 15,
        "ai_insight": 20,
        "research_article": 7,
        "simulation": 0
      },
      "by_date": {
        "2026-04-09": 8,
        "2026-04-08": 12
      },
      "most_opened": [
        { "popup_id": "quantum-dog", "popup_title": "...", "popup_type": "explainer", "count": 5 }
      ],
      "recent": [ { ...event... }, ... ]
    }

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/dashboard/me
    """
    uid = get_uid(payload)

    try:
        docs   = (
            db.collection("popup_events")
              .where("uid", "==", uid)
              .order_by("opened_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        events = [d.to_dict() for d in docs]
    except Exception as e:
        log.error("my_dashboard failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    # ── Aggregate stats ────────────────────────────────────────────────────

    # Count events per popup_type
    by_type: dict[str, int] = {t: 0 for t in VALID_POPUP_TYPES}
    for ev in events:
        t = ev.get("popup_type", "")
        if t in by_type:
            by_type[t] += 1

    # Count events per calendar date
    by_date: dict[str, int] = defaultdict(int)
    for ev in events:
        d = ev.get("date", "")
        if d:
            by_date[d] += 1

    # Most opened items (by popup_id)
    item_counts: dict[str, dict] = {}
    for ev in events:
        pid = ev.get("popup_id", "")
        if pid:
            if pid not in item_counts:
                item_counts[pid] = {
                    "popup_id":    pid,
                    "popup_title": ev.get("popup_title", ""),
                    "popup_type":  ev.get("popup_type", ""),
                    "count":       0,
                }
            item_counts[pid]["count"] += 1

    # Sort most-opened items descending by count, take top 10
    most_opened = sorted(item_counts.values(), key=lambda x: x["count"], reverse=True)[:10]

    return {
        "total_events": len(events),
        "by_type":      by_type,
        "by_date":      dict(by_date),
        "most_opened":  most_opened,
        "recent":       events[:20],   # last 20 events for activity feed
    }


@router.get(
    "/admin",
    summary = "Platform-wide dashboard stats — admin only",
)
def admin_dashboard(payload: dict = Depends(require_admin)):
    """
    GET /api/dashboard/admin
    Platform-wide activity statistics. ADMIN ONLY.

    Returns:
    - total events across all users
    - unique users who opened popups
    - breakdown by popup type
    - breakdown by date (last 30 days)
    - top 10 most popular items

    curl -H "Authorization: Bearer <admin_token>" http://127.0.0.1:5000/api/dashboard/admin
    """
    try:
        # Fetch last 2000 events for aggregation
        docs   = (
            db.collection("popup_events")
              .order_by("opened_at", direction="DESCENDING")
              .limit(2000)
              .stream()
        )
        events = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    # ── Aggregate ──────────────────────────────────────────────────────────

    unique_users: set[str] = set()
    by_type:  dict[str, int] = {t: 0 for t in VALID_POPUP_TYPES}
    by_date:  dict[str, int] = defaultdict(int)
    item_counts: dict[str, dict] = {}

    for ev in events:
        uid = ev.get("uid", "")
        if uid:
            unique_users.add(uid)

        t = ev.get("popup_type", "")
        if t in by_type:
            by_type[t] += 1

        d = ev.get("date", "")
        if d:
            by_date[d] += 1

        pid = ev.get("popup_id", "")
        if pid:
            if pid not in item_counts:
                item_counts[pid] = {
                    "popup_id":    pid,
                    "popup_title": ev.get("popup_title", ""),
                    "popup_type":  ev.get("popup_type", ""),
                    "count":       0,
                }
            item_counts[pid]["count"] += 1

    top_items = sorted(item_counts.values(), key=lambda x: x["count"], reverse=True)[:10]

    return {
        "total_events":  len(events),
        "unique_users":  len(unique_users),
        "by_type":       by_type,
        "by_date":       dict(sorted(by_date.items(), reverse=True)[:30]),  # last 30 days
        "top_items":     top_items,
    }


@router.get(
    "/admin/events",
    summary = "Raw event log — admin only",
)
def admin_events(
    limit:      int = Query(100, ge=1, le=500),
    popup_type: str = Query("", description="Filter by popup_type"),
    uid_filter: str = Query("", description="Filter by user uid"),
    payload:    dict = Depends(require_admin),
):
    """
    GET /api/dashboard/admin/events
    Raw paginated event log. ADMIN ONLY.
    Optional filters: popup_type, uid.

    curl -H "Authorization: Bearer <admin_token>" \\
      "http://127.0.0.1:5000/api/dashboard/admin/events?popup_type=ai_insight&limit=50"
    """
    try:
        docs   = (
            db.collection("popup_events")
              .order_by("opened_at", direction="DESCENDING")
              .limit(limit)
              .stream()
        )
        events = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    # Apply optional in-memory filters
    if popup_type.strip():
        events = [e for e in events if e.get("popup_type") == popup_type.strip()]
    if uid_filter.strip():
        events = [e for e in events if e.get("uid") == uid_filter.strip()]

    return {"events": events, "total": len(events)}