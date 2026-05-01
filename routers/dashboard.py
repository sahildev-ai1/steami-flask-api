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
  GET  /api/dashboard/subject-intelligence — per-subject engagement scores (requires auth)
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

from mongodb_client import db
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

# All STEAMI STEM subjects — must stay in sync with the interests picker on the frontend
ALL_SUBJECTS: list[str] = [
    "PHYSICS",
    "CHEMISTRY",
    "BIOLOGY",
    "MATHEMATICS",
    "COMPUTER SCIENCE",
    "AI + ROBOTICS",
    "SPACE + ASTRONOMY",
    "ENGINEERING",
    "ENVIRONMENT",
    "MEDICINE",
    "NEUROSCIENCE",
    "QUANTUM",
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


def _normalise_subject(raw: str) -> str | None:
    """
    Case-insensitive match of a raw field/topic string against ALL_SUBJECTS.
    Returns the canonical subject string or None if no match.
    Examples:
      "physics"          → "PHYSICS"
      "AI & Robotics"    → "AI + ROBOTICS"
      "machine learning" → "AI + ROBOTICS"
      "space"            → "SPACE + ASTRONOMY"
    """
    if not raw:
        return None
    upper = raw.upper().strip()

    # Direct match
    if upper in ALL_SUBJECTS:
        return upper

    # Alias / partial-match table
    ALIASES: dict[str, str] = {
        "AI":                   "AI + ROBOTICS",
        "ROBOTICS":             "AI + ROBOTICS",
        "AI & ROBOTICS":        "AI + ROBOTICS",
        "MACHINE LEARNING":     "AI + ROBOTICS",
        "DEEP LEARNING":        "AI + ROBOTICS",
        "CS":                   "COMPUTER SCIENCE",
        "COMPUTING":            "COMPUTER SCIENCE",
        "PROGRAMMING":          "COMPUTER SCIENCE",
        "SPACE":                "SPACE + ASTRONOMY",
        "ASTRONOMY":            "SPACE + ASTRONOMY",
        "ASTROPHYSICS":         "SPACE + ASTRONOMY",
        "ASTRO":                "SPACE + ASTRONOMY",
        "ENVIRONMENT":          "ENVIRONMENT",
        "ENVIRONMENTAL":        "ENVIRONMENT",
        "CLIMATE":              "ENVIRONMENT",
        "ECOLOGY":              "ENVIRONMENT",
        "BIO":                  "BIOLOGY",
        "LIFE SCIENCES":        "BIOLOGY",
        "CHEM":                 "CHEMISTRY",
        "MATH":                 "MATHEMATICS",
        "MATHS":                "MATHEMATICS",
        "NEURO":                "NEUROSCIENCE",
        "BRAIN":                "NEUROSCIENCE",
        "MED":                  "MEDICINE",
        "HEALTH":               "MEDICINE",
        "HEALTHCARE":           "MEDICINE",
        "QUANTUM COMPUTING":    "QUANTUM",
        "QUANTUM PHYSICS":      "QUANTUM",
        "QUANTUM MECHANICS":    "QUANTUM",
        "ENGINEER":             "ENGINEERING",
    }
    if upper in ALIASES:
        return ALIASES[upper]

    # Substring scan (longest-match wins)
    best_match = None
    best_len   = 0
    for subject in ALL_SUBJECTS:
        if subject in upper or upper in subject:
            if len(subject) > best_len:
                best_match = subject
                best_len   = len(subject)
    return best_match


def _build_subject_scores(events: list[dict], user_interests: list[str]) -> list[dict]:
    """
    Given a list of popup_events for a user, fetch the field/topic of each
    referenced content item and tally opens per subject.

    Strategy (in order, first match wins for each event):
      1. Look up the popup_id in `explainers` collection → use `field`
      2. Look up in `research_articles` collection → use `field`
      3. Look up in `ai_insights` collection → use `topic` or `matched_domains[0]`
      4. Look up in `articles` collection → use `topic` or `matched_domains[0]`
      5. Fall back to normalising popup_title words against ALL_SUBJECTS

    Returns a list of dicts like:
      [{ "subject": "PHYSICS", "opens": 12, "score": 85, "is_interest": True }, ...]
    sorted by score descending, all 12 subjects always present.
    """
    raw_counts: dict[str, int] = defaultdict(int)

    # Pre-group events by popup_type for efficient batch lookups
    by_type: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        by_type[ev.get("popup_type", "")].append(ev)

    # ── 1. Explainers ────────────────────────────────────────────────────────
    for ev in by_type.get("explainer", []):
        popup_id = ev.get("popup_id", "")
        subject  = None
        if popup_id:
            try:
                doc = db.collection("explainers").document(popup_id).get()
                if doc.exists:
                    subject = _normalise_subject(doc.to_dict().get("field", ""))
            except Exception:
                pass
        if not subject:
            subject = _normalise_subject(ev.get("popup_title", ""))
        if subject:
            raw_counts[subject] += 1

    # ── 2. Research articles ─────────────────────────────────────────────────
    for ev in by_type.get("research_article", []):
        popup_id = ev.get("popup_id", "")
        subject  = None
        if popup_id:
            try:
                doc = db.collection("research_articles").document(popup_id).get()
                if doc.exists:
                    subject = _normalise_subject(doc.to_dict().get("field", ""))
            except Exception:
                pass
        if not subject:
            subject = _normalise_subject(ev.get("popup_title", ""))
        if subject:
            raw_counts[subject] += 1

    # ── 3. AI Insights ───────────────────────────────────────────────────────
    for ev in by_type.get("ai_insight", []):
        popup_id = ev.get("popup_id", "")
        subject  = None
        if popup_id:
            try:
                # ai_insights keyed by article_id
                doc = db.collection("ai_insights").document(popup_id).get()
                if doc.exists:
                    d       = doc.to_dict()
                    subject = _normalise_subject(d.get("topic", ""))
                    if not subject:
                        domains = d.get("matched_domains", [])
                        for dm in domains:
                            subject = _normalise_subject(dm)
                            if subject:
                                break
            except Exception:
                pass
        if not subject:
            subject = _normalise_subject(ev.get("popup_title", ""))
        if subject:
            raw_counts[subject] += 1

    # ── 4. Simulations — use title heuristic only ────────────────────────────
    for ev in by_type.get("simulation", []):
        subject = _normalise_subject(ev.get("popup_title", ""))
        if subject:
            raw_counts[subject] += 1

    # ── 5. Build scored list (all 12 subjects always present) ────────────────
    interests_set = {i.upper().strip() for i in user_interests}
    max_opens     = max(raw_counts.values(), default=1)

    results = []
    for subject in ALL_SUBJECTS:
        opens = raw_counts.get(subject, 0)
        # Score: 0–100, scaled against the user's most-opened subject
        # Interests give a small base bonus (5 pts) so they appear meaningful
        # even before the user has opened anything in that subject.
        interest_bonus = 5 if subject in interests_set else 0
        raw_score      = (opens / max_opens) * 95 + interest_bonus if max_opens > 0 else interest_bonus
        score          = min(100, round(raw_score))

        results.append({
            "subject":     subject,
            "opens":       opens,
            "score":       score,
            "is_interest": subject in interests_set,
        })

    return sorted(results, key=lambda x: (x["score"], x["opens"]), reverse=True)


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
    popup_title: str = ""      # optional display title


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
    payload: dict = Depends(require_auth),
):
    """
    POST /api/dashboard/event
    Called by the frontend every time a user opens any popup.

    Body:
    {
      "popup_type":  "explainer",
      "popup_id":    "quantum-dog",
      "popup_title": "The Quantum Dog: Schrödinger's Pet Paradox"
    }
    """
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
        "opened_at":   now.isoformat(),
        "date":        now.strftime("%Y-%m-%d"),
        "hour":        now.hour,
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
    Returns the current user's full dashboard data:
    - recent events list (with date and time)
    - counts per popup type
    - counts per day (for a simple calendar/streak view)
    - most opened items
    - user interests (from profile)
    - insight stats (total insights available, with_insight count)

    Response:
    {
      "total_events": 42,
      "by_type": { "explainer": 15, "ai_insight": 20, ... },
      "by_date": { "2026-04-09": 8, ... },
      "most_opened": [ { "popup_id": "...", "popup_title": "...", "popup_type": "...", "count": 5 } ],
      "recent": [ { ...event... }, ... ],
      "interests": ["PHYSICS", "AI + ROBOTICS"],
      "insight_stats": {
        "total_insights": 32,
        "articles_with_insight": 28,
        "articles_total": 40,
        "generating": false
      },
      "diary_total": 7
    }
    """
    uid = get_uid(payload)

    # ── Popup events ──────────────────────────────────────────────────────────
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
        log.error("my_dashboard events failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    # ── Aggregate event stats ─────────────────────────────────────────────────
    by_type: dict[str, int] = {t: 0 for t in VALID_POPUP_TYPES}
    for ev in events:
        t = ev.get("popup_type", "")
        if t in by_type:
            by_type[t] += 1

    by_date: dict[str, int] = defaultdict(int)
    for ev in events:
        d = ev.get("date", "")
        if d:
            by_date[d] += 1

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

    most_opened = sorted(item_counts.values(), key=lambda x: x["count"], reverse=True)[:10]

    # ── User interests ────────────────────────────────────────────────────────
    interests: list[str] = []
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            interests = user_doc.to_dict().get("interests", [])
    except Exception as e:
        log.warning("my_dashboard: could not load interests uid=%s: %s", uid, e)

    # ── Insight stats (platform-wide, for the insight summary widget) ─────────
    insight_stats = {
        "total_insights": 0,
        "articles_with_insight": 0,
        "articles_total": 0,
        "generating": False,
    }
    try:
        art_docs  = db.collection("articles").stream()
        articles  = [d.to_dict() for d in art_docs]
        ins_docs  = db.collection("ai_insights").stream()
        insight_stats["total_insights"]       = sum(1 for _ in ins_docs)
        insight_stats["articles_total"]       = len(articles)
        insight_stats["articles_with_insight"] = sum(1 for a in articles if a.get("has_insight"))
    except Exception as e:
        log.warning("my_dashboard: insight stats failed: %s", e)

    # ── Diary total ───────────────────────────────────────────────────────────
    diary_total = 0
    try:
        diary_docs  = db.collection("diary").where("uid", "==", uid).stream()
        diary_total = sum(1 for _ in diary_docs)
    except Exception as e:
        log.warning("my_dashboard: diary count failed uid=%s: %s", uid, e)

    return {
        "total_events": len(events),
        "by_type":      by_type,
        "by_date":      dict(by_date),
        "most_opened":  most_opened,
        "recent":       events[:20],
        # ── new fields ──
        "interests":    interests,
        "insight_stats": insight_stats,
        "diary_total":  diary_total,
    }


@router.get(
    "/subject-intelligence",
    summary = "Per-subject engagement scores for the SubjectRadarChart — requires auth",
)
def subject_intelligence(
    limit:   int = Query(200, ge=1, le=500, description="Max popup events to analyse"),
    payload: dict = Depends(require_auth),
):
    """
    GET /api/dashboard/subject-intelligence

    Powers the Subject Intelligence radar chart on the dashboard.

    For every popup event the user has opened, this endpoint resolves the
    STEAMI subject/field of that content item (by looking it up in the
    `explainers`, `research_articles`, or `ai_insights` Firestore collections)
    and tallies how many times the user engaged with each subject.

    Scores are normalised to 0–100.  A user's saved interests receive a small
    base bonus (5 pts) so they show up even before any content has been opened.

    All 12 STEAMI subjects are always returned so the radar chart always has
    a full set of axes.

    Response:
    {
      "subjects": [
        {
          "subject":     "PHYSICS",
          "opens":       14,
          "score":       95,
          "is_interest": true
        },
        ...                          // all 12 subjects, sorted by score desc
      ],
      "total_events_analysed": 87,
      "top_subject": "PHYSICS",
      "user_interests": ["PHYSICS", "AI + ROBOTICS"]
    }
    """
    uid = get_uid(payload)

    # ── Fetch popup events ────────────────────────────────────────────────────
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
        log.error("subject_intelligence: event fetch failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    # ── Fetch user interests ──────────────────────────────────────────────────
    user_interests: list[str] = []
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            user_interests = user_doc.to_dict().get("interests", [])
    except Exception as e:
        log.warning("subject_intelligence: interests fetch failed uid=%s: %s", uid, e)

    # ── Build per-subject scores ──────────────────────────────────────────────
    subjects = _build_subject_scores(events, user_interests)

    top_subject = subjects[0]["subject"] if subjects and subjects[0]["opens"] > 0 else None

    log.info(
        "subject_intelligence: uid=%s events=%d top=%s",
        uid, len(events), top_subject,
    )

    return {
        "subjects":               subjects,
        "total_events_analysed":  len(events),
        "top_subject":            top_subject,
        "user_interests":         user_interests,
    }


@router.get(
    "/admin",
    summary = "Platform-wide dashboard stats — admin only",
)
def admin_dashboard(payload: dict = Depends(require_admin)):
    """
    GET /api/dashboard/admin
    Platform-wide activity statistics. ADMIN ONLY.
    """
    try:
        docs   = (
            db.collection("popup_events")
              .order_by("opened_at", direction="DESCENDING")
              .limit(2000)
              .stream()
        )
        events = [d.to_dict() for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    unique_users: set[str]   = set()
    by_type:      dict[str, int] = {t: 0 for t in VALID_POPUP_TYPES}
    by_date:      dict[str, int] = defaultdict(int)
    item_counts:  dict[str, dict] = {}

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
        "by_date":       dict(sorted(by_date.items(), reverse=True)[:30]),
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

    if popup_type.strip():
        events = [e for e in events if e.get("popup_type") == popup_type.strip()]
    if uid_filter.strip():
        events = [e for e in events if e.get("uid") == uid_filter.strip()]

    return {"events": events, "total": len(events)}