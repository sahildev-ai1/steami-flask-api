"""
routers/diary.py  —  Personal Research Diary API
==================================================
Allows authenticated users to save selected text/content from any popup
in the STEAMI app into their personal diary.

POPUP TYPES (what can be saved to the diary):
  research_article  — a card from the Research page
  ai_insight        — the AI-generated insight panel for a news article
  explainer         — an explainer article (quantum, CRISPR, neural nets, etc.)
  simulation        — a 3D simulation (future — placeholder type supported now)

DESIGN:
  - Each diary entry belongs to a specific user (uid in JWT)
  - An entry stores: the selected text, the popup type, the source item ID,
    a title, optional note, and timestamps
  - Users can only read/delete their own entries
  - Admins can read all entries

Firestore collection: `diary`
Fields: id, uid, popup_type, popup_id, title, selected_text, note,
        created_at, updated_at

ENDPOINTS:
  POST   /api/diary              — save a diary entry (requires auth)
  GET    /api/diary              — list own entries, newest first (requires auth)
  GET    /api/diary/{entry_id}   — get single entry (requires auth)
  PUT    /api/diary/{entry_id}   — update note (requires auth)
  DELETE /api/diary/{entry_id}   — delete own entry (requires auth)
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from mongodb_client import db
from auth import require_auth, require_admin, get_uid

log    = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# All valid popup types that can generate a diary entry.
# "simulation" is included now so the frontend can save it even before
# the simulation API is built.
VALID_POPUP_TYPES: list[str] = [
    "research_article",  # from the Research Articles page
    "ai_insight",        # from the AI Insight panel on a news article
    "explainer",         # from the Explainers page
    "simulation",        # from a 3D Simulation (future feature)
]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_owner(entry: dict, uid: str, role: str) -> None:
    """
    Verify the requesting user owns this diary entry.
    Admins can access any entry.
    Raises HTTP 403 if access is denied.
    """
    if entry.get("uid") != uid and role != "admin":
        raise HTTPException(403, detail="Access denied. This is not your diary entry.")


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class CreateDiaryBody(BaseModel):
    """
    Save a piece of content to the diary.

    popup_type   — type of popup where the content was saved from
    popup_id     — the ID of the source item (research article ID, insight ID, etc.)
    title        — short label for this entry (e.g. article title)
    selected_text — the actual text the user highlighted / wants to save
    note         — optional personal note the user adds
    """
    popup_type:    str            # one of VALID_POPUP_TYPES
    popup_id:      str            # ID of the source (article_id, explainer_id, etc.)
    title:         str            # display title for the diary entry
    selected_text: str            # the content being saved
    note:          str  = ""      # optional personal annotation


class UpdateDiaryBody(BaseModel):
    """Only the personal note can be updated after creation."""
    note:  Optional[str] = None
    title: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "",
    status_code = 201,
    summary     = "Save a diary entry — requires auth",
)
def create_diary_entry(
    body:    CreateDiaryBody,
    payload: dict = Depends(require_auth),  # any logged-in user
):
    """
    POST /api/diary
    Save selected content from any popup to the personal diary.

    Body:
    {
      "popup_type":    "ai_insight",
      "popup_id":      "article-uuid-here",
      "title":         "AI Health Tools Article",
      "selected_text": "AI health chatbots are rapidly gaining popularity...",
      "note":          "Interesting point about lack of clinical testing"
    }

    popup_type options:
      research_article | ai_insight | explainer | simulation

    Response:
    {
      "id":            "diary-entry-uuid",
      "uid":           "user-uuid",
      "popup_type":    "ai_insight",
      "popup_id":      "article-uuid",
      "title":         "AI Health Tools Article",
      "selected_text": "...",
      "note":          "Interesting point...",
      "created_at":    "2026-04-09T...",
      "updated_at":    "2026-04-09T..."
    }

    curl -X POST http://127.0.0.1:5000/api/diary \\
      -H "Authorization: Bearer <token>" \\
      -H "Content-Type: application/json" \\
      -d '{"popup_type":"explainer","popup_id":"quantum-dog","title":"Quantum Dog","selected_text":"Superposition allows..."}'
    """
    # Validate popup type
    if body.popup_type not in VALID_POPUP_TYPES:
        raise HTTPException(
            400,
            detail=f"Invalid popup_type. Must be one of: {', '.join(VALID_POPUP_TYPES)}"
        )

    # selected_text must not be empty
    if not body.selected_text.strip():
        raise HTTPException(400, detail="selected_text cannot be empty.")

    uid      = get_uid(payload)
    entry_id = str(uuid.uuid4())

    entry = {
        "id":            entry_id,
        "uid":           uid,                         # owner of this entry
        "popup_type":    body.popup_type,             # what type of popup it came from
        "popup_id":      body.popup_id.strip(),       # source item ID
        "title":         body.title.strip(),
        "selected_text": body.selected_text.strip(),  # the saved content
        "note":          body.note.strip(),            # personal annotation
        "created_at":    _now(),
        "updated_at":    _now(),
    }

    try:
        db.collection("diary").document(entry_id).set(entry)
    except Exception as e:
        log.error("create_diary_entry failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    log.info("diary saved: uid=%s type=%s popup_id=%s", uid, body.popup_type, body.popup_id)
    return entry


@router.get(
    "",
    summary = "List own diary entries — requires auth",
)
def list_diary_entries(
    limit:      int = Query(50, ge=1, le=200),
    popup_type: str = Query("", description="Filter by popup_type (optional)"),
    payload:    dict = Depends(require_auth),
):
    """
    GET /api/diary?limit=50&popup_type=ai_insight
    List all diary entries for the current user, newest first.
    Optional filter by popup_type.

    Response:
    {
      "entries": [ { ...diary entry... }, ... ],
      "total":   12
    }

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/diary
    curl -H "Authorization: Bearer <token>" "http://127.0.0.1:5000/api/diary?popup_type=explainer"
    """
    uid = get_uid(payload)

    try:
        # Filter by uid — each user only sees their own entries
        q = (
            db.collection("diary")
              .where("uid", "==", uid)
              .order_by("created_at", direction="DESCENDING")
              .limit(limit)
        )
        docs    = q.stream()
        entries = [d.to_dict() for d in docs]

        # Optional in-memory filter by popup_type (Firestore REST doesn't support
        # compound indexes on where+where without pre-built indexes)
        if popup_type.strip():
            entries = [e for e in entries if e.get("popup_type") == popup_type.strip()]

    except Exception as e:
        log.error("list_diary_entries failed uid=%s: %s", uid, e)
        raise HTTPException(500, detail=str(e))

    return {"entries": entries, "total": len(entries)}


@router.get(
    "/{entry_id}",
    summary = "Get single diary entry — requires auth",
)
def get_diary_entry(
    entry_id: str,
    payload:  dict = Depends(require_auth),
):
    """
    GET /api/diary/{entry_id}
    Get a specific diary entry. Only the owner (or admin) can access it.

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/diary/ENTRY_ID
    """
    doc = db.collection("diary").document(entry_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="Diary entry not found.")

    entry = doc.to_dict()
    _check_owner(entry, get_uid(payload), payload.get("role", "user"))
    return entry


@router.put(
    "/{entry_id}",
    summary = "Update diary entry note/title — requires auth",
)
def update_diary_entry(
    entry_id: str,
    body:     UpdateDiaryBody,
    payload:  dict = Depends(require_auth),
):
    """
    PUT /api/diary/{entry_id}
    Update the personal note or title on a diary entry.
    Only the owner can update their own entries.

    Body: { "note": "Updated annotation", "title": "New title" }

    curl -X PUT http://127.0.0.1:5000/api/diary/ENTRY_ID \\
      -H "Authorization: Bearer <token>" \\
      -d '{"note":"Updated thought"}'
    """
    doc_ref = db.collection("diary").document(entry_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Diary entry not found.")

    entry = doc.to_dict()
    _check_owner(entry, get_uid(payload), payload.get("role", "user"))

    # Build update dict — only update fields that were provided
    updates: dict = {"updated_at": _now()}
    if body.note  is not None: updates["note"]  = body.note.strip()
    if body.title is not None: updates["title"] = body.title.strip()

    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    return {"updated": True, "entry_id": entry_id}


@router.delete(
    "/{entry_id}",
    summary = "Delete diary entry — requires auth",
)
def delete_diary_entry(
    entry_id: str,
    payload:  dict = Depends(require_auth),
):
    """
    DELETE /api/diary/{entry_id}
    Delete a diary entry. Only the owner (or admin) can delete it.

    curl -X DELETE http://127.0.0.1:5000/api/diary/ENTRY_ID \\
      -H "Authorization: Bearer <token>"
    """
    doc_ref = db.collection("diary").document(entry_id)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="Diary entry not found.")

    _check_owner(doc.to_dict(), get_uid(payload), payload.get("role", "user"))

    try:
        doc_ref.delete()
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("diary deleted: entry_id=%s by uid=%s", entry_id, get_uid(payload))
    return {"deleted": True, "entry_id": entry_id}