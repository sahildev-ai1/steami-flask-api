"""
routers/profile_router.py  —  User Profile Management  v1
===========================================================
All endpoints are under:  /api/profile

This router handles everything a user can do to their own account:
  - View their full profile
  - Update name, username, bio, location, website, profession, interests
  - Change their email (requires password confirmation)
  - Change their password (requires current password)
  - Set / update / remove avatar via URL link
  - Delete their own account (requires password confirmation)

ENDPOINTS:
  GET    /api/profile/me                 ← get own full profile (auth)
  PATCH  /api/profile/me                 ← update basic info: name, username, bio, etc (auth)
  PATCH  /api/profile/me/email           ← change email, requires password (auth)
  PATCH  /api/profile/me/password        ← change password, requires current password (auth)
  PATCH  /api/profile/me/avatar          ← set/update avatar URL (auth)
  DELETE /api/profile/me/avatar          ← remove avatar (auth)
  DELETE /api/profile/me                 ← delete own account, requires password (auth)

VALIDATION:
  - Username: 3-30 chars, alphanumeric + underscores only, must be unique
  - Avatar: must be a valid http/https URL ending in a known image extension
             OR a URL from a known image host (imgur, cloudinary, gravatar, etc.)
  - Password: min 8 chars, must contain uppercase, lowercase, and a digit
  - Email: basic format check + uniqueness check

LIBRARIES USED:
  - fastapi      — routing, deps, HTTP exceptions (already in your stack)
  - pydantic     — request body validation (already in your stack)
  - re           — regex for username + password strength (stdlib)
  - urllib.parse — URL parsing for avatar validation (stdlib)
  - No new pip installs needed.

FRONTEND INTEGRATION GUIDE (at bottom of file as a docstring)
"""

import re
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator

from mongodb_client import db
from auth import (
    hash_password,
    verify_password,
    require_auth,
    get_uid,
)

log    = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

VALID_PROFESSIONS: list[str] = [
    "student",
    "working_professional",
    "professor",
    "researcher",
    "self_learner",
    "educator",
    "other",
]

VALID_TOPICS: list[str] = [
    "PHYSICS",
    "CHEMISTRY",
    "BIOLOGY",
    "MEDICINE",
    "EARTH & SPACE",
    "COMPUTER SCIENCE",
    "AI + ROBOTICS",
    "ENGINEERING",
    "MATHEMATICS & DATA",
    "CLIMATE & ENERGY",
]

# Known image CDN/hosting domains — avatar URLs from these are always accepted
# even if they don't end in a known image extension (e.g. Gravatar hash URLs)
TRUSTED_IMAGE_HOSTS: set[str] = {
    "i.imgur.com",
    "imgur.com",
    "res.cloudinary.com",
    "www.gravatar.com",
    "gravatar.com",
    "lh3.googleusercontent.com",   # Google profile photos
    "avatars.githubusercontent.com",  # GitHub avatars
    "pbs.twimg.com",               # Twitter/X profile images
    "cdn.discordapp.com",
    "media.licdn.com",             # LinkedIn
    "graph.facebook.com",
    "s3.amazonaws.com",
    "storage.googleapis.com",
    "imagedelivery.net",           # Cloudflare Images
}

# Known image file extensions
IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"}

# Username rules
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")

# Password strength rule: min 8 chars, 1 uppercase, 1 lowercase, 1 digit
PASSWORD_MIN_LEN = 8
PASSWORD_RE      = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _safe_user(user: dict) -> dict:
    """Strip sensitive fields before returning user data to the client."""
    return {k: v for k, v in user.items() if k not in ("password_hash",)}


def _find_by_email(email: str) -> Optional[dict]:
    """Lookup user by email. Returns dict or None."""
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    return docs[0].to_dict() if docs else None


def _find_by_username(username: str) -> Optional[dict]:
    """Lookup user by username. Returns dict or None."""
    docs = db.collection("users").where("username", "==", username).limit(1).stream()
    return docs[0].to_dict() if docs else None


def _validate_avatar_url(url: str) -> str:
    """
    Validate that a URL is a plausible image URL.
    Accepts:
      1. URLs from TRUSTED_IMAGE_HOSTS (any path)
      2. Any http/https URL whose path ends in a known IMAGE_EXTENSION
    Raises ValueError with a clear message on failure.
    Returns the cleaned URL on success.
    """
    url = url.strip()
    if not url:
        raise ValueError("Avatar URL cannot be empty.")

    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Could not parse the avatar URL.")

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Avatar URL must start with http:// or https://")

    hostname = parsed.hostname or ""

    # Check trusted hosts first
    if hostname in TRUSTED_IMAGE_HOSTS:
        return url

    # Check if the path ends with a known image extension
    path = parsed.path.lower().split("?")[0]   # ignore query params for extension check
    if any(path.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return url

    raise ValueError(
        "Avatar URL must be a direct image link (ending in .jpg, .png, .webp, etc.) "
        "or from a trusted image host (Imgur, Cloudinary, Gravatar, etc.)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST BODY SCHEMAS
# ─────────────────────────────────────────────────────────────────────────────

class UpdateProfileBody(BaseModel):
    """
    PATCH /api/profile/me
    All fields are optional — only the fields you send will be updated.
    """
    full_name:       Optional[str]       = None   # Display name, 1-80 chars
    username:        Optional[str]       = None   # Unique handle, 3-30 chars, a-z0-9_
    bio:             Optional[str]       = None   # Short bio, max 300 chars
    location:        Optional[str]       = None   # e.g. "Mumbai, India"
    website:         Optional[str]       = None   # Personal website URL
    profession:      Optional[str]       = None   # One of VALID_PROFESSIONS
    interests:       Optional[list[str]] = None   # Subset of VALID_TOPICS
    subscribe_email: Optional[bool]      = None   # Email digest preference

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v):
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("full_name cannot be empty.")
            if len(v) > 80:
                raise ValueError("full_name must be 80 characters or fewer.")
        return v

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        if v is not None:
            v = v.strip().lower()
            if not USERNAME_RE.match(v):
                raise ValueError(
                    "Username must be 3-30 characters and contain only "
                    "letters, numbers, and underscores."
                )
        return v

    @field_validator("bio")
    @classmethod
    def validate_bio(cls, v):
        if v is not None and len(v) > 300:
            raise ValueError("Bio must be 300 characters or fewer.")
        return v

    @field_validator("website")
    @classmethod
    def validate_website(cls, v):
        if v:
            v = v.strip()
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https"):
                raise ValueError("Website must be a valid http/https URL.")
        return v


class ChangeEmailBody(BaseModel):
    """PATCH /api/profile/me/email"""
    new_email:        str   # The new email address
    current_password: str   # Must confirm identity with current password

    @field_validator("new_email")
    @classmethod
    def validate_email(cls, v):
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email format.")
        return v


class ChangePasswordBody(BaseModel):
    """PATCH /api/profile/me/password"""
    current_password: str   # Old password to confirm identity
    new_password:     str   # Must meet strength requirements

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v):
        if not PASSWORD_RE.match(v):
            raise ValueError(
                "Password must be at least 8 characters and include "
                "an uppercase letter, a lowercase letter, and a digit."
            )
        return v

    @model_validator(mode="after")
    def passwords_must_differ(self):
        if self.current_password == self.new_password:
            raise ValueError("New password must be different from the current password.")
        return self


class SetAvatarBody(BaseModel):
    """PATCH /api/profile/me/avatar"""
    avatar_url: str   # Direct image URL or link from a trusted host

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar(cls, v):
        return _validate_avatar_url(v)  # raises ValueError on bad URL


class DeleteAccountBody(BaseModel):
    """DELETE /api/profile/me"""
    current_password: str   # Require password to confirm irreversible action


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me", summary="Get own profile — requires auth")
def get_my_profile(payload: dict = Depends(require_auth)):
    """
    Returns the authenticated user's full profile (password hash excluded).

    Frontend usage:
      GET /api/profile/me
      Authorization: Bearer <token>
    """
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    return {"user": _safe_user(doc.to_dict())}


# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/me", summary="Update basic profile info — requires auth")
def update_profile(body: UpdateProfileBody, payload: dict = Depends(require_auth)):
    """
    Update one or more profile fields in a single request.
    Only send the fields you want to change — all others are left untouched.

    Fields you can update here:
      full_name, username, bio, location, website, profession,
      interests, subscribe_email

    Frontend usage:
      PATCH /api/profile/me
      Authorization: Bearer <token>
      Content-Type: application/json
      { "full_name": "Sahil Sharma", "bio": "AI enthusiast 🚀" }
    """
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    user    = doc.to_dict()
    updates: dict = {}

    if body.full_name is not None:
        updates["full_name"] = body.full_name

    if body.username is not None:
        # Check uniqueness — username must not be taken by another user
        existing = _find_by_username(body.username)
        if existing and existing.get("id") != uid:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Username '@{body.username}' is already taken."
            )
        updates["username"] = body.username

    if body.bio is not None:
        updates["bio"] = body.bio.strip()

    if body.location is not None:
        updates["location"] = body.location.strip()

    if body.website is not None:
        updates["website"] = body.website

    if body.profession is not None:
        updates["profession"] = body.profession.strip()

    if body.interests is not None:
        invalid = [t for t in body.interests if t not in VALID_TOPICS]
        if invalid:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid topics: {invalid}. Valid options: {VALID_TOPICS}"
            )
        updates["interests"] = list(dict.fromkeys(body.interests))  # deduplicate, preserve order

    if body.subscribe_email is not None:
        updates["subscribe_email"] = body.subscribe_email

    if not updates:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="No fields provided to update."
        )

    updates["updated_at"] = _now()

    try:
        doc_ref.update(updates)
    except Exception as e:
        log.error("update_profile: uid=%s error=%s", uid, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    updated_doc  = doc_ref.get()
    changed      = [k for k in updates if k != "updated_at"]

    log.info("update_profile: uid=%s changed=%s", uid, changed)
    return {
        "updated":        True,
        "updated_fields": changed,
        "user":           _safe_user(updated_doc.to_dict()),
    }


# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/me/email", summary="Change email address — requires auth + password")
def change_email(body: ChangeEmailBody, payload: dict = Depends(require_auth)):
    """
    Change the user's email address.
    Requires the current password to confirm identity.
    The new email must not already be in use by another account.

    Frontend usage:
      PATCH /api/profile/me/email
      Authorization: Bearer <token>
      { "new_email": "newemail@example.com", "current_password": "MyPass123" }
    """
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    user = doc.to_dict()

    # Block Google/OAuth users — they authenticate via OAuth and have no password
    if user.get("auth_provider") in ("google", "github", "apple"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Your account uses Google sign-in. Password management is not available for OAuth accounts."
        )

    # Verify current password
    if not verify_password(body.current_password, user.get("password_hash", "")):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect."
        )

    new_email = body.new_email

    # Check it's not the same email
    if new_email == user.get("email", "").lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="New email is the same as the current email."
        )

    # Check uniqueness across all users
    existing = _find_by_email(new_email)
    if existing and existing.get("id") != uid:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This email address is already registered to another account."
        )

    try:
        doc_ref.update({"email": new_email, "updated_at": _now()})
    except Exception as e:
        log.error("change_email: uid=%s error=%s", uid, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    log.info("change_email: uid=%s new_email=%s", uid, new_email)
    return {
        "updated": True,
        "email":   new_email,
        "message": "Email updated successfully. Please use your new email to log in.",
    }


# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/me/password", summary="Change password — requires auth + current password")
def change_password(body: ChangePasswordBody, payload: dict = Depends(require_auth)):
    """
    Change the user's password.
    Requires the current password.
    New password must be ≥8 chars with uppercase, lowercase, and a digit.

    Frontend usage:
      PATCH /api/profile/me/password
      Authorization: Bearer <token>
      { "current_password": "OldPass1", "new_password": "NewPass2025!" }
    """
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    user = doc.to_dict()

    # Block Google/OAuth users — they authenticate via OAuth and have no password
    if user.get("auth_provider") in ("google", "github", "apple"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Your account uses Google sign-in. Password management is not available for OAuth accounts."
        )

    # Verify current password
    if not verify_password(body.current_password, user.get("password_hash", "")):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect."
        )

    new_hash = hash_password(body.new_password)

    try:
        doc_ref.update({"password_hash": new_hash, "updated_at": _now()})
    except Exception as e:
        log.error("change_password: uid=%s error=%s", uid, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    log.info("change_password: uid=%s", uid)
    return {
        "updated": True,
        "message": "Password changed successfully. Your existing sessions remain valid.",
    }


# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/me/avatar", summary="Set or update avatar via image URL — requires auth")
def set_avatar(body: SetAvatarBody, payload: dict = Depends(require_auth)):
    """
    Set or replace the user's avatar using an image URL.

    Accepted URLs:
      • Direct image links ending in .jpg .jpeg .png .gif .webp .avif .svg
      • Links from trusted hosts: Imgur, Cloudinary, Gravatar, GitHub, Google, etc.

    Example accepted URLs:
      https://i.imgur.com/abc123.jpg
      https://res.cloudinary.com/demo/image/upload/sample.jpg
      https://www.gravatar.com/avatar/abc123?s=200
      https://avatars.githubusercontent.com/u/12345678
      https://example.com/photos/myface.png

    Frontend usage:
      PATCH /api/profile/me/avatar
      Authorization: Bearer <token>
      { "avatar_url": "https://i.imgur.com/abc123.jpg" }
    """
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)

    if not doc_ref.get().exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    try:
        doc_ref.update({"avatar_url": body.avatar_url, "updated_at": _now()})
    except Exception as e:
        log.error("set_avatar: uid=%s error=%s", uid, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    log.info("set_avatar: uid=%s url=%s", uid, body.avatar_url)
    return {
        "updated":    True,
        "avatar_url": body.avatar_url,
        "message":    "Avatar updated successfully.",
    }


# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/me/avatar", summary="Remove avatar — requires auth")
def remove_avatar(payload: dict = Depends(require_auth)):
    """
    Remove the user's avatar (sets avatar_url to null).

    Frontend usage:
      DELETE /api/profile/me/avatar
      Authorization: Bearer <token>
    """
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)

    if not doc_ref.get().exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    try:
        doc_ref.update({"avatar_url": None, "updated_at": _now()})
    except Exception as e:
        log.error("remove_avatar: uid=%s error=%s", uid, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    log.info("remove_avatar: uid=%s", uid)
    return {"updated": True, "avatar_url": None, "message": "Avatar removed."}


# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/me", summary="Delete own account — requires auth + password")
def delete_my_account(
    payload:          dict                       = Depends(require_auth),
    body:             Optional[DeleteAccountBody] = None,
    current_password: Optional[str]              = None,
):
    """
    Permanently delete the authenticated user's account.
    Accepts password as JSON body { "current_password": "..." }
    OR as query param ?current_password=... (fallback for clients that strip DELETE bodies).
    """
    # Accept password from JSON body OR query param
    password = (body.current_password if body else None) or current_password
    if not password:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="current_password is required (send as JSON body or ?current_password= query param)."
        )

    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    user = doc.to_dict()

    # Block Google/OAuth users — they have no password_hash
    if user.get("auth_provider") in ("google", "github", "apple"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Your account uses Google sign-in. To delete your account, please contact support."
        )

    if not verify_password(password, user.get("password_hash", "")):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Password is incorrect. Account deletion cancelled."
        )

    try:
        doc_ref.delete()
    except Exception as e:
        log.error("delete_account: uid=%s error=%s", uid, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    log.info("delete_account: uid=%s email=%s", uid, user.get("email"))
    return {
        "deleted": True,
        "message": "Your account has been permanently deleted.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# FRONTEND INTEGRATION GUIDE
# ─────────────────────────────────────────────────────────────────────────────
"""
REGISTERING THE ROUTER IN main.py
──────────────────────────────────
  from routers.profile_router import router as profile_router
  app.include_router(profile_router, prefix="/api/profile", tags=["Profile"])


NEW USER FIELDS ADDED TO THE users COLLECTION
──────────────────────────────────────────────
  Field          Type            Description
  ─────────────  ──────────────  ─────────────────────────────────────────
  username       str | null      Unique handle (e.g. "sahil_42")
  bio            str | null      Short bio, max 300 chars
  location       str | null      Free-text location (e.g. "Delhi, India")
  website        str | null      Personal website URL
  avatar_url     str | null      Direct link to profile image

  Existing fields (already in your schema) are unchanged.


API SUMMARY FOR FRONTEND DEVELOPERS
──────────────────────────────────────
  Method   Path                      Body fields                     Auth
  ──────   ───────────────────────   ──────────────────────────────  ──────
  GET      /api/profile/me           —                               Bearer
  PATCH    /api/profile/me           full_name, username, bio,       Bearer
                                     location, website, profession,
                                     interests, subscribe_email
  PATCH    /api/profile/me/email     new_email, current_password     Bearer
  PATCH    /api/profile/me/password  current_password, new_password  Bearer
  PATCH    /api/profile/me/avatar    avatar_url                      Bearer
  DELETE   /api/profile/me/avatar    —                               Bearer
  DELETE   /api/profile/me          current_password                 Bearer


REACT EXAMPLE — Update profile
───────────────────────────────
  const updateProfile = async (fields) => {
    const res = await fetch('/api/profile/me', {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify(fields),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    return data.user;
  };

  // Usage — only send what changed:
  await updateProfile({ bio: "AI enthusiast 🚀", location: "Delhi" });


REACT EXAMPLE — Set avatar
───────────────────────────
  const setAvatar = async (url) => {
    const res = await fetch('/api/profile/me/avatar', {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ avatar_url: url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    return data.avatar_url;
  };


REACT EXAMPLE — Change password
────────────────────────────────
  const changePassword = async (currentPassword, newPassword) => {
    const res = await fetch('/api/profile/me/password', {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail);
    return data.message;
  };


VALIDATION RULES (mirror these in your frontend for instant feedback)
───────────────────────────────────────────────────────────────────────
  username      3–30 chars, only letters/numbers/underscores, unique
  full_name     1–80 chars
  bio           max 300 chars
  website       must start with http:// or https://
  avatar_url    http/https URL ending in .jpg/.png/.webp/.gif/.svg/.avif
                OR from: imgur, cloudinary, gravatar, github, google, etc.
  new_password  ≥8 chars, 1 uppercase, 1 lowercase, 1 digit

PASSWORD STRENGTH REGEX (copy to frontend):
  /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$/

USERNAME REGEX (copy to frontend):
  /^[a-zA-Z0-9_]{3,30}$/
"""