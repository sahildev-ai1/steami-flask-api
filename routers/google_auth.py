"""
routers/google_auth.py  —  Google OAuth Sign-In / Sign-Up
===========================================================

NEW ENDPOINTS:
  POST /api/auth/google              — Sign in or sign up with a Google ID token
  PATCH /api/auth/profile            — Update fields not available from Google
  GET  /api/auth/profile             — Get own full profile (any auth)

HOW GOOGLE AUTH WORKS:
  1. Frontend gets a Google ID token via Google Sign-In button.
  2. Frontend sends { "id_token": "<google-id-token>" } to POST /api/auth/google.
  3. Backend verifies the token with Google's tokeninfo endpoint.
  4. If the email already exists → log in (return existing user + STEAMI JWT).
  5. If the email is new → create account automatically (no password needed).
  6. Returns same shape as /api/auth/login: { token, uid, email, role, ... }

PATCH /api/auth/profile  lets users fill in:
  - display_name   (if they want a custom name instead of Google's)
  - profession     (student / researcher / educator / professional / other)
  - interests      (list of STEM topics)
  - bio            (short bio)
  - avatar_url     (custom avatar — defaults to Google photo)
"""

import uuid
import logging
import requests as _requests

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from mongodb_client import db
from auth import require_auth, get_uid, create_jwt

log = logging.getLogger(__name__)
router = APIRouter()

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


# ── Helpers ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _verify_google_token(id_token: str) -> dict:
    """
    Verify a Google ID token and return the decoded payload.
    Raises HTTPException 401 if the token is invalid.
    """
    try:
        resp = _requests.get(
            GOOGLE_TOKENINFO_URL,
            params={"id_token": id_token},
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(401, detail="Invalid Google ID token")
        data = resp.json()
        # Sanity check: must have an email
        if "email" not in data:
            raise HTTPException(401, detail="Google token missing email claim")
        return data
    except HTTPException:
        raise
    except Exception as e:
        log.error("Google token verification failed: %s", e)
        raise HTTPException(401, detail=f"Could not verify Google token: {e}")


def _get_or_create_google_user(google_payload: dict) -> dict:
    """
    Look up user by Google email. Create if not found.
    Returns the full user document.
    """
    email = google_payload["email"].lower().strip()
    google_uid = google_payload.get("sub", "")          # Google's unique user ID
    name = google_payload.get("name", "")
    picture = google_payload.get("picture", "")
    email_verified = google_payload.get("email_verified", "false") == "true"

    # ── Check if user already exists by email ──────────────────────────────
    try:
        existing_docs = (
            db.collection("users")
              .where("email", "==", email)
              .limit(1)
              .stream()
        )
        existing = list(existing_docs)
    except Exception as e:
        raise HTTPException(500, detail=f"DB lookup failed: {e}")

    if existing:
        user_doc = existing[0].to_dict()
        uid = user_doc["uid"]

        # Update Google fields in case they changed (name, picture)
        try:
            db.collection("users").document(uid).update({
                "google_uid":      google_uid,
                "google_picture":  picture,
                "email_verified":  email_verified,
                "last_login":      _now_iso(),
                "auth_provider":   "google",
            })
        except Exception as e:
            log.warning("Failed to update google fields for %s: %s", uid, e)

        user_doc.update({
            "google_uid":     google_uid,
            "google_picture": picture,
            "last_login":     _now_iso(),
        })
        return user_doc

    # ── New user — create account ──────────────────────────────────────────
    uid = str(uuid.uuid4())
    new_user = {
        "uid":            uid,
        "email":          email,
        "display_name":   name,
        "avatar_url":     picture,
        "google_uid":     google_uid,
        "google_picture": picture,
        "email_verified": email_verified,
        "auth_provider":  "google",
        "role":           "user",
        "profession":     "",
        "bio":            "",
        "interests":      [],
        "subscribed_newsletter": True,   # opt-in by default on Google signup
        "created_at":     _now_iso(),
        "last_login":     _now_iso(),
    }
    try:
        db.collection("users").document(uid).set(new_user)
        # Also add to newsletter subscribers
        db.collection("newsletter_subscribers").document(uid).set({
            "uid":        uid,
            "email":      email,
            "name":       name,
            "subscribed": True,
            "source":     "google_signup",
            "created_at": _now_iso(),
        })
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to create user: {e}")

    log.info("New Google user created: uid=%s email=%s", uid, email)
    return new_user


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST BODIES
# ══════════════════════════════════════════════════════════════════════════════

class GoogleSignInBody(BaseModel):
    """Body for POST /api/auth/google"""
    id_token: str


class PatchProfileBody(BaseModel):
    """Body for PATCH /api/auth/profile — all fields optional"""
    display_name:           Optional[str]       = None
    profession:             Optional[str]       = None   # student/researcher/educator/professional/other
    bio:                    Optional[str]       = None
    avatar_url:             Optional[str]       = None
    interests:              Optional[list[str]] = None   # STEM topic list
    subscribed_newsletter:  Optional[bool]      = None


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/auth/google  — Sign in or sign up with Google
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/google",
    summary="Sign in or sign up with Google ID token — PUBLIC",
    tags=["Auth"],
)
def google_sign_in(body: GoogleSignInBody):
    """
    **Public endpoint.** Sign in or create an account using a Google ID token.

    The frontend must obtain a Google ID token via the Google Sign-In SDK
    (e.g., `google.accounts.id.initialize` / `googleUser.credential`).

    Flow:
    1. Token is verified with Google's tokeninfo endpoint.
    2. If the email already exists in STEAMI → returns existing user + JWT.
    3. If the email is new → creates account (role=user) + returns JWT.

    Body:
    ```json
    { "id_token": "<google-id-token>" }
    ```

    Response (same shape as POST /api/auth/login):
    ```json
    {
      "token":        "<steami-jwt>",
      "uid":          "uuid",
      "email":        "user@gmail.com",
      "display_name": "Jane Doe",
      "role":         "user",
      "avatar_url":   "https://lh3.googleusercontent.com/...",
      "is_new_user":  true
    }
    ```

    After receiving the token, check `is_new_user`:
    - If `true` → redirect to onboarding (PATCH /api/auth/profile to set profession/bio).
    - If `false` → redirect to main app.
    """
    # 1. Verify token with Google
    google_payload = _verify_google_token(body.id_token)

    # 2. Get or create user
    is_new = not bool(
        list(
            db.collection("users")
              .where("email", "==", google_payload["email"].lower())
              .limit(1)
              .stream()
        )
    )
    user = _get_or_create_google_user(google_payload)

    # 3. Issue STEAMI JWT (same mechanism as email/password login)
    token = create_jwt(uid=user["uid"], role=user.get("role", "user"))

    log.info(
        "google_sign_in: uid=%s email=%s new=%s",
        user["uid"], user["email"], is_new,
    )

    return {
        "token":        token,
        "uid":          user["uid"],
        "email":        user["email"],
        "display_name": user.get("display_name", ""),
        "role":         user.get("role", "user"),
        "avatar_url":   user.get("avatar_url", ""),
        "profession":   user.get("profession", ""),
        "interests":    user.get("interests", []),
        "is_new_user":  is_new,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PATCH /api/auth/profile  — Update fields not available from Google
# ══════════════════════════════════════════════════════════════════════════════

@router.patch(
    "/profile",
    summary="Update profile fields (profession, bio, interests, etc.) — requires auth",
    tags=["Auth"],
)
def patch_profile(
    body:    PatchProfileBody,
    payload: dict = Depends(require_auth),
):
    """
    **Requires login.** Update profile fields that Google doesn't provide.

    All fields are optional — only send the ones you want to change.

    Typically called right after Google sign-in for new users (onboarding step).

    Body (all optional):
    ```json
    {
      "display_name":          "Jane Doe",
      "profession":            "student",
      "bio":                   "I love STEM!",
      "avatar_url":            "https://...",
      "interests":             ["AI + ROBOTICS", "PHYSICS"],
      "subscribed_newsletter": true
    }
    ```

    Profession values: `student` | `researcher` | `educator` | `professional` | `other`
    """
    uid = get_uid(payload)

    # Build update dict from only provided fields
    updates: dict = {"updated_at": _now_iso()}

    if body.display_name is not None:
        updates["display_name"] = body.display_name.strip()

    if body.profession is not None:
        valid_professions = {"student", "researcher", "educator", "professional", "other"}
        if body.profession not in valid_professions:
            raise HTTPException(
                400,
                detail=f"profession must be one of: {', '.join(sorted(valid_professions))}",
            )
        updates["profession"] = body.profession

    if body.bio is not None:
        updates["bio"] = body.bio.strip()[:500]   # cap at 500 chars

    if body.avatar_url is not None:
        updates["avatar_url"] = body.avatar_url.strip()

    if body.interests is not None:
        updates["interests"] = body.interests

    if body.subscribed_newsletter is not None:
        updates["subscribed_newsletter"] = body.subscribed_newsletter
        # Sync to newsletter_subscribers collection
        try:
            db.collection("newsletter_subscribers").document(uid).update({
                "subscribed":   body.subscribed_newsletter,
                "updated_at":   _now_iso(),
            })
        except Exception:
            # Document might not exist yet; create it
            user_doc = db.collection("users").document(uid).get()
            if user_doc.exists:
                u = user_doc.to_dict()
                db.collection("newsletter_subscribers").document(uid).set({
                    "uid":        uid,
                    "email":      u.get("email", ""),
                    "name":       u.get("display_name", ""),
                    "subscribed": body.subscribed_newsletter,
                    "source":     "profile_update",
                    "created_at": _now_iso(),
                })

    if len(updates) == 1:   # only updated_at — nothing to do
        raise HTTPException(400, detail="No valid fields provided to update")

    try:
        db.collection("users").document(uid).update(updates)
    except Exception as e:
        raise HTTPException(500, detail=f"Profile update failed: {e}")

    # Return updated profile
    doc = db.collection("users").document(uid).get()
    user = doc.to_dict() if doc.exists else {}
    user.pop("password_hash", None)   # never expose password hash

    log.info("patch_profile: uid=%s fields=%s", uid, list(updates.keys()))
    return {"updated": True, "profile": user}


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/auth/profile  — Get own full profile
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/profile",
    summary="Get own full profile — requires auth",
    tags=["Auth"],
)
def get_profile(payload: dict = Depends(require_auth)):
    """
    **Requires login.** Get your own full profile.

    Returns all fields including profession, bio, interests, newsletter status.
    """
    uid = get_uid(payload)
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found")
    user = doc.to_dict()
    user.pop("password_hash", None)
    return {"profile": user}