"""
routers/dashboard.py  —  Activity Dashboard API  (v2 — Enriched)
=================================================================
Tracks every time a user opens a popup in the STEAMI app.
This data powers the admin dashboard, user activity view, and the
upcoming recommendation system.

HOW IT WORKS (v2):
  1. Frontend calls POST /api/dashboard/event with popup_type + popup_id.
  2. The API fetches the full content of that item from our own collections
     (explainers / research_articles / ai_insights / articles / simulations).
  3. Keywords are extracted from the content using TF-IDF-style term scoring
     against a curated STEM vocabulary.
  4. The subject is resolved via _normalise_subject (same as before).
  5. The enriched event is stored in MongoDB's `popup_events` collection with
     the new fields: keywords, subject, content_snippet.
  6. An admin-only endpoint generates a CSV export of all enriched events —
     ready to feed into a recommendation pipeline.

POPUP TYPES tracked:
  research_article | ai_insight | explainer | simulation

ENDPOINTS (unchanged):
  POST /api/dashboard/event              — log a popup open (requires auth)
  GET  /api/dashboard/me                 — own activity summary (requires auth)
  GET  /api/dashboard/subject-intelligence — per-subject engagement scores (requires auth)
  GET  /api/dashboard/admin              — platform-wide stats (admin only)
  GET  /api/dashboard/admin/events       — raw event log (admin only)

ENDPOINTS (new):
  GET  /api/dashboard/admin/export-csv   — full enriched event CSV (admin only)
  GET  /api/dashboard/admin/user-profiles — per-user interest profile (admin only)
  GET  /api/dashboard/admin/content-heatmap — content × subject engagement matrix (admin only)

MongoDB collection: `popup_events`
Fields (v2): id, uid, popup_type, popup_id, popup_title, opened_at, date, hour,
             subject, keywords (list[str]), content_snippet (str),
             read_duration_seconds (int | null), device_type (str)
"""

import csv
import io
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mongodb_client import db
from auth import require_auth, require_admin, get_uid

log    = logging.getLogger(__name__)
router = APIRouter()

# ── Constants ────────────────────────────────────────────────────────────────

VALID_POPUP_TYPES: list[str] = [
    "research_article",
    "ai_insight",
    "explainer",
    "simulation",
]

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

# STEM keyword vocabulary used for keyword extraction.
# Maps raw terms → canonical STEM keyword tag.
# Extend this as new content domains are added.
STEM_VOCABULARY: dict[str, str] = {
    # Physics
    "quantum":         "quantum mechanics",
    "superposition":   "quantum mechanics",
    "entanglement":    "quantum entanglement",
    "relativity":      "relativity",
    "gravity":         "gravity",
    "gravitational":   "gravity",
    "particle":        "particle physics",
    "photon":          "photon",
    "electron":        "electron",
    "neutron":         "neutron",
    "proton":          "proton",
    "wave":            "wave physics",
    "thermodynamic":   "thermodynamics",
    "entropy":         "thermodynamics",
    "magnetic":        "magnetism",
    "optic":           "optics",
    "laser":           "optics",
    "nuclear":         "nuclear physics",
    "plasma":          "plasma physics",
    "dark matter":     "dark matter",
    "dark energy":     "dark energy",
    # Chemistry
    "molecule":        "molecular chemistry",
    "atom":            "atomic structure",
    "reaction":        "chemical reaction",
    "catalyst":        "catalysis",
    "polymer":         "polymer chemistry",
    "organic":         "organic chemistry",
    "synthesis":       "chemical synthesis",
    "protein":         "biochemistry",
    "enzyme":          "biochemistry",
    "acid":            "acid-base chemistry",
    "bond":            "chemical bonding",
    "crystal":         "crystallography",
    # Biology
    "dna":             "genetics",
    "gene":            "genetics",
    "crispr":          "gene editing",
    "genome":          "genomics",
    "cell":            "cell biology",
    "evolution":       "evolutionary biology",
    "species":         "taxonomy",
    "bacteria":        "microbiology",
    "virus":           "virology",
    "ecosystem":       "ecology",
    "photosynthesis":  "photosynthesis",
    "stem cell":       "stem cell research",
    "mutation":        "mutation",
    # Mathematics
    "algorithm":       "algorithms",
    "theorem":         "mathematics",
    "calculus":        "calculus",
    "probability":     "probability",
    "statistic":       "statistics",
    "topology":        "topology",
    "fractal":         "fractals",
    "prime":           "number theory",
    "matrix":          "linear algebra",
    "vector":          "linear algebra",
    "differential":    "differential equations",
    "graph":           "graph theory",
    # Computer Science
    "software":        "software engineering",
    "hardware":        "hardware",
    "compiler":        "compilers",
    "database":        "databases",
    "network":         "networking",
    "cybersecurity":   "cybersecurity",
    "encryption":      "cryptography",
    "blockchain":      "blockchain",
    "cloud":           "cloud computing",
    "microprocessor":  "microprocessors",
    "operating system":"operating systems",
    "data structure":  "data structures",
    # AI + Robotics
    "neural":          "neural networks",
    "machine learning":"machine learning",
    "deep learning":   "deep learning",
    "transformer":     "transformer architecture",
    "llm":             "large language models",
    "language model":  "large language models",
    "computer vision": "computer vision",
    "reinforcement":   "reinforcement learning",
    "robot":           "robotics",
    "autonomous":      "autonomous systems",
    "gpt":             "large language models",
    "diffusion model": "generative AI",
    "generative":      "generative AI",
    # Space + Astronomy
    "planet":          "planetary science",
    "star":            "stellar physics",
    "galaxy":          "galactic astronomy",
    "telescope":       "observational astronomy",
    "black hole":      "black holes",
    "nasa":            "space exploration",
    "rocket":          "rocketry",
    "mars":            "Mars exploration",
    "orbit":           "orbital mechanics",
    "solar":           "solar physics",
    "exoplanet":       "exoplanets",
    "universe":        "cosmology",
    "comet":           "small solar system bodies",
    # Engineering
    "bridge":          "structural engineering",
    "circuit":         "electrical engineering",
    "sensor":          "sensor technology",
    "turbine":         "mechanical engineering",
    "material":        "materials science",
    "nanotechnology":  "nanotechnology",
    "semiconductor":   "semiconductor engineering",
    "battery":         "energy storage",
    "solar panel":     "photovoltaics",
    "3d print":        "additive manufacturing",
    # Environment
    "climate":         "climate science",
    "carbon":          "carbon cycle",
    "emission":        "emissions",
    "renewable":       "renewable energy",
    "fossil fuel":     "fossil fuels",
    "biodiversity":    "biodiversity",
    "deforestation":   "deforestation",
    "pollution":       "pollution",
    "ocean":           "oceanography",
    "glacier":         "glaciology",
    "sustainability":  "sustainability",
    # Medicine
    "vaccine":         "vaccines",
    "cancer":          "oncology",
    "antibiotic":      "antimicrobials",
    "clinical trial":  "clinical trials",
    "drug":            "pharmacology",
    "surgery":         "surgery",
    "pathogen":        "infectious disease",
    "immune":          "immunology",
    "neurodegenerative":"neurological disorders",
    "mental health":   "mental health",
    "diabetes":        "metabolic disease",
    "cardiovascular":  "cardiology",
    # Neuroscience
    "brain":           "brain science",
    "neuron":          "neuronal activity",
    "synapse":         "synaptic biology",
    "cognition":       "cognitive science",
    "consciousness":   "consciousness studies",
    "memory":          "memory research",
    "sleep":           "sleep science",
    "dopamine":        "neurotransmitters",
    "serotonin":       "neurotransmitters",
    "cortex":          "cortical function",
    "neuroplasticity": "neuroplasticity",
    # Quantum (dedicated)
    "qubit":           "quantum computing",
    "quantum computer":"quantum computing",
    "quantum error":   "quantum error correction",
    "quantum gate":    "quantum gates",
    "decoherence":     "quantum decoherence",
    "cryptograph":     "quantum cryptography",
    "quantum supremacy":"quantum supremacy",
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _hour_now() -> int:
    return datetime.now(timezone.utc).hour


def _normalise_subject(raw: str) -> str | None:
    """
    Case-insensitive match of a raw field/topic string against ALL_SUBJECTS.
    Returns the canonical subject string or None if no match.
    """
    if not raw:
        return None
    upper = raw.upper().strip()

    if upper in ALL_SUBJECTS:
        return upper

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

    best_match = None
    best_len   = 0
    for subject in ALL_SUBJECTS:
        if subject in upper or upper in subject:
            if len(subject) > best_len:
                best_match = subject
                best_len   = len(subject)
    return best_match


def _extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    """
    Extract STEM keywords from free text by scanning against STEM_VOCABULARY.
    Returns up to `max_keywords` canonical keyword tags, deduplicated.

    Strategy:
      1. Lowercase the text.
      2. Scan all multi-word vocab terms first (longest match wins).
      3. Then scan single-word terms.
      4. Deduplicate while preserving first-occurrence order.
    """
    if not text:
        return []

    lower_text = text.lower()
    found: list[str] = []
    seen_tags: set[str] = set()

    # Sort vocab keys longest-first so multi-word terms are matched before
    # their constituent single words (e.g. "black hole" before "hole").
    sorted_vocab = sorted(STEM_VOCABULARY.keys(), key=len, reverse=True)

    for term in sorted_vocab:
        if len(found) >= max_keywords:
            break
        # Use word-boundary matching so "star" doesn't match "standard"
        pattern = r'\b' + re.escape(term) + r'\b'
        if re.search(pattern, lower_text):
            tag = STEM_VOCABULARY[term]
            if tag not in seen_tags:
                seen_tags.add(tag)
                found.append(tag)

    return found


def _fetch_content(popup_type: str, popup_id: str) -> dict:
    """
    Fetch the content document for a given popup from our own API collections.

    Returns a dict with keys:
      - text       : str  — concatenated text for keyword extraction
      - field      : str  — raw field / topic string for subject normalisation
      - snippet    : str  — first 280 chars of content for the event record
      - title      : str  — content title (fallback label)

    Returns an empty dict on failure — the event is still logged, just without
    enrichment.
    """
    result = {"text": "", "field": "", "snippet": "", "title": ""}

    COLLECTION_MAP = {
        "explainer":        ("explainers",        "field"),
        "research_article": ("research_articles",  "field"),
        "ai_insight":       ("ai_insights",        "topic"),
        "simulation":       ("simulations",        "field"),
    }

    col_name, field_key = COLLECTION_MAP.get(popup_type, (None, None))
    if not col_name:
        return result

    try:
        doc = db.collection(col_name).document(popup_id).get()
        if not doc.exists:
            # Fallback: try `articles` collection (ai_insights may reference article_id)
            if popup_type == "ai_insight":
                doc = db.collection("articles").document(popup_id).get()
                field_key = "topic"
            if not doc.exists:
                return result

        data = doc.to_dict()
        result["field"] = data.get(field_key, "") or data.get("matched_domains", [""])[0] if isinstance(data.get("matched_domains"), list) and data.get("matched_domains") else data.get(field_key, "")
        result["title"] = data.get("title", "")

        # Gather all text fields for keyword extraction
        text_parts = []
        for key in ("title", "subtitle", "abstract", "content", "description",
                    "summary", "keyInsights", "keyFindings", "body_paragraphs",
                    "standfirst", "caption", "context", "technicalDetail", "impact"):
            val = data.get(key, "")
            if isinstance(val, list):
                text_parts.extend([str(v) for v in val if v])
            elif val:
                text_parts.append(str(val))

        full_text = " ".join(text_parts)
        result["text"]    = full_text
        result["snippet"] = full_text[:280].strip()

    except Exception as e:
        log.warning("_fetch_content failed type=%s id=%s: %s", popup_type, popup_id, e)

    return result


def _build_subject_scores(events: list[dict], user_interests: list[str]) -> list[dict]:
    """
    Given a list of popup_events, build per-subject engagement scores.
    Uses the pre-stored `subject` field on each event (set at log time)
    to avoid re-fetching content. Falls back to title heuristic.

    Returns a list sorted by score descending, all 12 subjects present.
    """
    raw_counts: dict[str, int] = defaultdict(int)

    for ev in events:
        subject = ev.get("subject") or _normalise_subject(ev.get("popup_title", ""))
        if subject:
            raw_counts[subject] += 1

    interests_set = {i.upper().strip() for i in user_interests}
    max_opens     = max(raw_counts.values(), default=1)

    results = []
    for subject in ALL_SUBJECTS:
        opens          = raw_counts.get(subject, 0)
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
    Sent by the frontend when a popup is OPENED.
    user_name / user_role are resolved server-side from the auth token.
    """
    popup_type:  str
    popup_id:    str
    popup_title: str = ""
    device_type: str = "unknown"   # mobile | desktop | tablet | unknown


class DurationPatchBody(BaseModel):
    """
    Sent by the frontend when a popup is CLOSED.
    PATCHes the existing open-event document with the read duration
    so there is exactly ONE row per open/close cycle — no duplicate rows.
    """
    read_duration_seconds: int


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

    Logs a popup-open event. Returns the full enriched event document
    including the auto-assigned event_id — the frontend stores this id
    and sends it to PATCH /event/{id}/duration when the popup closes.

    Body:
    {
      "popup_type":  "explainer",
      "popup_id":    "quantum-dog",
      "popup_title": "The Quantum Dog: Schrödinger's Pet Paradox",
      "device_type": "desktop"
    }

    NOTE: read_duration_seconds is NOT accepted here any more.
    Send it via PATCH /api/dashboard/event/{id}/duration on close.
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

    # ── Resolve user name + role from the users collection ───────────────────
    user_name = "Unknown"
    user_role = "user"
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            u = user_doc.to_dict()
            user_name = (
                u.get("full_name")
                or u.get("display_name")
                or u.get("name")
                or u.get("email", "Unknown")
            )
            user_role = u.get("role", "user") or "user"
    except Exception as e:
        log.warning("log_popup_event: user lookup failed uid=%s: %s", uid, e)

    # Normalise role to one of: admin | mod | user
    _ROLE_MAP = {"moderator": "mod", "administrator": "admin"}
    user_role = _ROLE_MAP.get(user_role.lower(), user_role.lower())
    if user_role not in ("admin", "mod", "user"):
        user_role = "user"

    # ── Resolve device type — fall back to "desktop" not "unknown" ────────────
    device_type = body.device_type if body.device_type in ("mobile", "tablet", "desktop") else "desktop"

    # ── Enrich: fetch content from our own API ────────────────────────────────
    content_data = _fetch_content(body.popup_type, body.popup_id.strip())

    # ── Extract keywords ──────────────────────────────────────────────────────
    combined_text = f"{body.popup_title} {content_data['text']}"
    keywords = _extract_keywords(combined_text)

    # ── Resolve subject ───────────────────────────────────────────────────────
    subject = (
        _normalise_subject(content_data["field"])
        or _normalise_subject(body.popup_title)
    )

    event = {
        # Core identity
        "id":  event_id,
        "uid": uid,

        # ── NEW: user identity columns ─────────────────────────────────────────
        "user_name": user_name,   # full name, email fallback, or "Unknown"
        "user_role": user_role,   # admin | mod | user

        # Popup metadata
        "popup_type":  body.popup_type,
        "popup_id":    body.popup_id.strip(),
        "popup_title": body.popup_title.strip() or content_data["title"],

        # Timestamps
        "opened_at": now.isoformat(),
        "date":      now.strftime("%Y-%m-%d"),
        "hour":      now.hour,
        "week":      now.isocalendar()[1],
        "month":     now.strftime("%Y-%m"),

        # Enrichment
        "subject":         subject,
        "keywords":        keywords,
        "content_snippet": content_data["snippet"],

        # Engagement — duration starts null, filled in by PATCH on close
        "read_duration_seconds": None,
        "device_type":           device_type,
    }

    try:
        db.collection("popup_events").document(event_id).set(event)
    except Exception as e:
        log.error("log_popup_event failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    log.info(
        "popup event: uid=%s type=%s id=%s subject=%s user=%s role=%s",
        uid, body.popup_type, body.popup_id, subject, user_name, user_role,
    )
    return event


@router.patch(
    "/event/{event_id}/duration",
    summary = "Record read duration when a popup closes — requires auth",
)
def patch_event_duration(
    event_id: str,
    body:     DurationPatchBody,
    payload:  dict = Depends(require_auth),
):
    """
    PATCH /api/dashboard/event/{event_id}/duration

    Called by the frontend when a popup is closed. Updates the existing
    open-event document with read_duration_seconds so there is exactly ONE
    row per open/close cycle — no duplicate rows in the CSV export.

    Body: { "read_duration_seconds": 142 }

    Rules:
    - Only the owner (uid match) or an admin can patch an event.
    - Duration must be >= 2 seconds (ignore accidental fast closes).
    - Duration capped at 7200 seconds (2 hours) to filter outliers.
    """
    if body.read_duration_seconds < 2:
        return {"ok": True, "skipped": "duration too short"}

    duration = min(body.read_duration_seconds, 7200)
    uid = get_uid(payload)

    try:
        doc_ref = db.collection("popup_events").document(event_id)
        doc     = doc_ref.get()
        if not doc.exists:
            raise HTTPException(404, detail="Event not found")

        ev = doc.to_dict()
        # Only allow owner or admin to patch
        if ev.get("uid") != uid:
            user_doc = db.collection("users").document(uid).get()
            role = user_doc.to_dict().get("role", "user") if user_doc.exists else "user"
            if role not in ("admin", "mod", "moderator"):
                raise HTTPException(403, detail="Not authorised to patch this event")

        doc_ref.update({"read_duration_seconds": duration})
        log.info("duration patched: event=%s uid=%s duration=%d", event_id, uid, duration)
        return {"ok": True, "event_id": event_id, "read_duration_seconds": duration}

    except HTTPException:
        raise
    except Exception as e:
        log.error("patch_event_duration failed event=%s: %s", event_id, e)
        raise HTTPException(500, detail=str(e))


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

    Returns the current user's full dashboard data including:
    - recent events (now with keywords + subject)
    - counts per popup type
    - counts per day (calendar/streak view)
    - most opened items
    - top keywords (aggregated across all events)
    - user interests (from profile)
    - insight stats
    - diary total

    Response shape (new fields marked with ★):
    {
      "total_events":   42,
      "by_type":        { "explainer": 15, ... },
      "by_date":        { "2026-04-09": 8, ... },
      "most_opened":    [ { "popup_id": "...", "count": 5 } ],
      "recent":         [ { ...event... }, ... ],
      "top_keywords":   ★ [ { "keyword": "quantum mechanics", "count": 9 }, ... ],
      "top_subjects":   ★ [ { "subject": "PHYSICS", "opens": 14 }, ... ],
      "streak_days":    ★ 7,
      "interests":      ["PHYSICS", "AI + ROBOTICS"],
      "insight_stats":  { ... },
      "diary_total":    7
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

    # ── Aggregate ─────────────────────────────────────────────────────────────
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
                    "subject":     ev.get("subject"),
                    "count":       0,
                }
            item_counts[pid]["count"] += 1

    most_opened = sorted(item_counts.values(), key=lambda x: x["count"], reverse=True)[:10]

    # ── Keyword frequency (for "your top topics" widget) ─────────────────────
    keyword_counts: dict[str, int] = defaultdict(int)
    for ev in events:
        for kw in ev.get("keywords", []):
            if kw:
                keyword_counts[kw] += 1
    top_keywords = sorted(
        [{"keyword": k, "count": v} for k, v in keyword_counts.items()],
        key=lambda x: x["count"], reverse=True
    )[:15]

    # ── Subject frequency ─────────────────────────────────────────────────────
    subject_counts: dict[str, int] = defaultdict(int)
    for ev in events:
        s = ev.get("subject")
        if s:
            subject_counts[s] += 1
    top_subjects = sorted(
        [{"subject": k, "opens": v} for k, v in subject_counts.items()],
        key=lambda x: x["opens"], reverse=True
    )

    # ── Streak calculation (consecutive days with at least 1 event) ───────────
    streak_days = 0
    if by_date:
        from datetime import timedelta
        check_date = datetime.now(timezone.utc).date()
        while check_date.strftime("%Y-%m-%d") in by_date:
            streak_days += 1
            check_date -= timedelta(days=1)

    # ── User interests ────────────────────────────────────────────────────────
    interests: list[str] = []
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            interests = user_doc.to_dict().get("interests", [])
    except Exception as e:
        log.warning("my_dashboard: could not load interests uid=%s: %s", uid, e)

    # ── Insight stats ─────────────────────────────────────────────────────────
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
        insight_stats["total_insights"]        = sum(1 for _ in ins_docs)
        insight_stats["articles_total"]        = len(articles)
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
        "total_events":  len(events),
        "by_type":       by_type,
        "by_date":       dict(by_date),
        "most_opened":   most_opened,
        "recent":        events[:20],
        # ── v2 additions ──
        "top_keywords":  top_keywords,
        "top_subjects":  top_subjects,
        "streak_days":   streak_days,
        # ── unchanged ──
        "interests":     interests,
        "insight_stats": insight_stats,
        "diary_total":   diary_total,
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

    Powers the Subject Intelligence radar chart. Uses pre-stored `subject`
    field from enriched events (no extra DB lookups needed in v2).

    All 12 STEAMI subjects always returned. Sorted by score descending.
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
        log.error("subject_intelligence: event fetch failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    user_interests: list[str] = []
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            user_interests = user_doc.to_dict().get("interests", [])
    except Exception as e:
        log.warning("subject_intelligence: interests fetch failed uid=%s: %s", uid, e)

    subjects    = _build_subject_scores(events, user_interests)
    top_subject = subjects[0]["subject"] if subjects and subjects[0]["opens"] > 0 else None

    # Build keyword breakdown per subject (for the recommendation engine)
    subject_keywords: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ev in events:
        s = ev.get("subject")
        if s:
            for kw in ev.get("keywords", []):
                if kw:
                    subject_keywords[s][kw] += 1

    # Top 5 keywords per subject
    subject_keyword_top = {
        s: sorted(
            [{"keyword": k, "count": v} for k, v in kws.items()],
            key=lambda x: x["count"], reverse=True
        )[:5]
        for s, kws in subject_keywords.items()
    }

    log.info(
        "subject_intelligence: uid=%s events=%d top=%s",
        uid, len(events), top_subject,
    )

    return {
        "subjects":               subjects,
        "total_events_analysed":  len(events),
        "top_subject":            top_subject,
        "user_interests":         user_interests,
        "subject_keywords":       subject_keyword_top,  # ★ new — for recommendation seeding
    }


@router.get(
    "/admin",
    summary = "Platform-wide dashboard stats — admin only",
)
def admin_dashboard(payload: dict = Depends(require_admin)):
    """
    GET /api/dashboard/admin
    Platform-wide activity statistics. ADMIN ONLY.
    Now includes keyword frequency and per-subject stats across all users.
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

    unique_users: set[str]       = set()
    by_type:      dict[str, int] = {t: 0 for t in VALID_POPUP_TYPES}
    by_date:      dict[str, int] = defaultdict(int)
    by_subject:   dict[str, int] = defaultdict(int)
    keyword_counts: dict[str, int] = defaultdict(int)
    item_counts:  dict[str, dict] = {}
    device_counts: dict[str, int] = defaultdict(int)

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

        s = ev.get("subject")
        if s:
            by_subject[s] += 1

        for kw in ev.get("keywords", []):
            if kw:
                keyword_counts[kw] += 1

        dev = ev.get("device_type", "unknown")
        device_counts[dev] += 1

        pid = ev.get("popup_id", "")
        if pid:
            if pid not in item_counts:
                item_counts[pid] = {
                    "popup_id":    pid,
                    "popup_title": ev.get("popup_title", ""),
                    "popup_type":  ev.get("popup_type", ""),
                    "subject":     ev.get("subject"),
                    "count":       0,
                }
            item_counts[pid]["count"] += 1

    top_items    = sorted(item_counts.values(), key=lambda x: x["count"], reverse=True)[:10]
    top_keywords = sorted(
        [{"keyword": k, "count": v} for k, v in keyword_counts.items()],
        key=lambda x: x["count"], reverse=True
    )[:20]

    return {
        "total_events":   len(events),
        "unique_users":   len(unique_users),
        "by_type":        by_type,
        "by_date":        dict(sorted(by_date.items(), reverse=True)[:30]),
        "by_subject":     dict(sorted(by_subject.items(), key=lambda x: x[1], reverse=True)),
        "top_items":      top_items,
        "top_keywords":   top_keywords,           # ★ new
        "device_counts":  dict(device_counts),    # ★ new
    }


@router.get(
    "/admin/events",
    summary = "Raw event log — admin only",
)
def admin_events(
    limit:       int = Query(100, ge=1, le=500),
    popup_type:  str = Query("", description="Filter by popup_type"),
    uid_filter:  str = Query("", description="Filter by user uid"),
    subject:     str = Query("", description="Filter by subject"),
    date_from:   str = Query("", description="Filter from date YYYY-MM-DD"),
    date_to:     str = Query("", description="Filter to date YYYY-MM-DD"),
    payload:     dict = Depends(require_admin),
):
    """
    GET /api/dashboard/admin/events
    Raw paginated event log. ADMIN ONLY.
    Now supports filtering by subject, date range in addition to popup_type / uid.
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
    if subject.strip():
        events = [e for e in events if e.get("subject") == subject.strip().upper()]
    if date_from.strip():
        events = [e for e in events if e.get("date", "") >= date_from.strip()]
    if date_to.strip():
        events = [e for e in events if e.get("date", "") <= date_to.strip()]

    return {"events": events, "total": len(events)}


# ─────────────────────────────────────────────────────────────────────────────
# NEW: CSV EXPORT  (admin only)
# ─────────────────────────────────────────────────────────────────────────────

# Columns in the CSV export.
# Ordered so that the most ML-useful fields come first.
_CSV_COLUMNS = [
    "id",
    "uid",
    "user_name",               # full name or "Unknown" for unauthenticated
    "user_role",               # admin | mod | user
    "opened_at",
    "date",
    "hour",
    "week",
    "month",
    "popup_type",
    "popup_id",
    "popup_title",
    "subject",
    "keywords",                # pipe-separated list: "quantum mechanics|black holes"
    "content_snippet",
    "read_duration_seconds",
    "device_type",
]


@router.get(
    "/admin/export-csv",
    summary = "Export all enriched popup events as CSV — admin only",
)
def admin_export_csv(
    popup_type:  str = Query("", description="Filter by popup_type"),
    uid_filter:  str = Query("", description="Filter by user uid"),
    subject:     str = Query("", description="Filter by subject"),
    date_from:   str = Query("", description="Filter from date YYYY-MM-DD"),
    date_to:     str = Query("", description="Filter to date YYYY-MM-DD"),
    limit:       int = Query(10000, ge=1, le=50000, description="Max rows"),
    payload:     dict = Depends(require_admin),
):
    """
    GET /api/dashboard/admin/export-csv

    Downloads a CSV file of all enriched popup events.
    Designed for feeding directly into a recommendation pipeline or BI tool.

    Query parameters (all optional filters):
      popup_type  — research_article | ai_insight | explainer | simulation
      uid_filter  — specific user UID
      subject     — STEAMI subject name e.g. PHYSICS
      date_from   — YYYY-MM-DD (inclusive)
      date_to     — YYYY-MM-DD (inclusive)
      limit       — max rows (default 10 000, max 50 000)

    CSV columns:
      id, uid, opened_at, date, hour, week, month,
      popup_type, popup_id, popup_title,
      subject, keywords (pipe-separated), content_snippet,
      read_duration_seconds, device_type

    The `keywords` column is pipe-separated (|) so it survives standard
    CSV parsing even when keywords contain spaces.
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

    # Apply filters
    if popup_type.strip():
        events = [e for e in events if e.get("popup_type") == popup_type.strip()]
    if uid_filter.strip():
        events = [e for e in events if e.get("uid") == uid_filter.strip()]
    if subject.strip():
        events = [e for e in events if e.get("subject") == subject.strip().upper()]
    if date_from.strip():
        events = [e for e in events if e.get("date", "") >= date_from.strip()]
    if date_to.strip():
        events = [e for e in events if e.get("date", "") <= date_to.strip()]

    # Build CSV in-memory
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames  = _CSV_COLUMNS,
        extrasaction= "ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()

    for ev in events:
        # Serialise keywords list → pipe-separated string
        kw_list = ev.get("keywords", [])
        row = {
            **ev,
            "keywords": "|".join(kw_list) if kw_list else "",
        }
        writer.writerow(row)

    output.seek(0)
    filename = f"steami_events_{_today()}.csv"

    log.info("admin_export_csv: %d rows exported", len(events))

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type = "text/csv",
        headers    = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Rows":        str(len(events)),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# NEW: PER-USER INTEREST PROFILES  (admin only)
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/admin/user-profiles",
    summary = "Per-user aggregated interest profiles — admin only",
)
def admin_user_profiles(
    limit: int = Query(5000, ge=1, le=20000),
    payload: dict = Depends(require_admin),
):
    """
    GET /api/dashboard/admin/user-profiles

    Aggregates all popup events into per-user interest profiles.
    Essential for the offline recommendation model:
      - top subjects per user
      - top keywords per user
      - engagement recency (last active date)
      - total events, total read time

    Response:
    {
      "profiles": [
        {
          "uid":                "abc123",
          "total_events":       42,
          "top_subject":        "PHYSICS",
          "subject_counts":     { "PHYSICS": 14, "QUANTUM": 9, ... },
          "top_keywords":       [ { "keyword": "quantum mechanics", "count": 9 }, ... ],
          "last_active":        "2026-05-18",
          "total_read_seconds": 3240,
          "device_preference":  "desktop"
        },
        ...
      ],
      "total_users": 87
    }
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

    # Aggregate per user
    user_data: dict[str, dict] = {}

    for ev in events:
        uid = ev.get("uid", "")
        if not uid:
            continue

        if uid not in user_data:
            user_data[uid] = {
                "uid":             uid,
                "total_events":    0,
                "subject_counts":  defaultdict(int),
                "keyword_counts":  defaultdict(int),
                "device_counts":   defaultdict(int),
                "last_active":     "",
                "total_read_seconds": 0,
            }

        u = user_data[uid]
        u["total_events"] += 1

        s = ev.get("subject")
        if s:
            u["subject_counts"][s] += 1

        for kw in ev.get("keywords", []):
            if kw:
                u["keyword_counts"][kw] += 1

        dev = ev.get("device_type", "unknown")
        u["device_counts"][dev] += 1

        ev_date = ev.get("date", "")
        if ev_date > u["last_active"]:
            u["last_active"] = ev_date

        dur = ev.get("read_duration_seconds")
        if isinstance(dur, int) and dur > 0:
            u["total_read_seconds"] += dur

    # Convert defaultdicts to serialisable dicts + compute derived fields
    profiles = []
    for uid, u in user_data.items():
        subject_counts = dict(u["subject_counts"])
        keyword_counts = dict(u["keyword_counts"])
        device_counts  = dict(u["device_counts"])

        top_subject = max(subject_counts, key=subject_counts.get) if subject_counts else None
        top_keywords = sorted(
            [{"keyword": k, "count": v} for k, v in keyword_counts.items()],
            key=lambda x: x["count"], reverse=True
        )[:10]
        device_preference = max(device_counts, key=device_counts.get) if device_counts else "unknown"

        profiles.append({
            "uid":                uid,
            "total_events":       u["total_events"],
            "top_subject":        top_subject,
            "subject_counts":     subject_counts,
            "top_keywords":       top_keywords,
            "last_active":        u["last_active"],
            "total_read_seconds": u["total_read_seconds"],
            "device_preference":  device_preference,
        })

    # Sort by most active users first
    profiles.sort(key=lambda x: x["total_events"], reverse=True)

    return {
        "profiles":    profiles,
        "total_users": len(profiles),
    }


# ─────────────────────────────────────────────────────────────────────────────
# NEW: CONTENT × SUBJECT ENGAGEMENT HEATMAP  (admin only)
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/admin/content-heatmap",
    summary = "Content × subject engagement matrix — admin only",
)
def admin_content_heatmap(
    limit:   int = Query(2000, ge=1, le=10000),
    payload: dict = Depends(require_admin),
):
    """
    GET /api/dashboard/admin/content-heatmap

    Returns an engagement matrix useful for:
    - Identifying which content items drive the most engagement per subject
    - Spotting gaps (subjects with few popular items)
    - Cold-start seeding for the recommendation system

    Response:
    {
      "heatmap": {
        "PHYSICS": [
          { "popup_id": "quantum-dog", "popup_title": "...", "popup_type": "explainer", "opens": 23 },
          ...
        ],
        ...
      },
      "subject_totals": { "PHYSICS": 87, "QUANTUM": 43, ... },
      "unclassified":   12
    }
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

    # subject → popup_id → {title, type, opens}
    heatmap: dict[str, dict[str, dict]] = {s: {} for s in ALL_SUBJECTS}
    subject_totals: dict[str, int] = defaultdict(int)
    unclassified = 0

    for ev in events:
        s   = ev.get("subject")
        pid = ev.get("popup_id", "")
        if not s or s not in heatmap or not pid:
            unclassified += 1
            continue

        subject_totals[s] += 1
        if pid not in heatmap[s]:
            heatmap[s][pid] = {
                "popup_id":    pid,
                "popup_title": ev.get("popup_title", ""),
                "popup_type":  ev.get("popup_type", ""),
                "opens":       0,
            }
        heatmap[s][pid]["opens"] += 1

    # Sort each subject's items by opens desc, keep top 20
    sorted_heatmap = {
        s: sorted(items.values(), key=lambda x: x["opens"], reverse=True)[:20]
        for s, items in heatmap.items()
    }

    return {
        "heatmap":        sorted_heatmap,
        "subject_totals": dict(sorted(subject_totals.items(), key=lambda x: x[1], reverse=True)),
        "unclassified":   unclassified,
    }
