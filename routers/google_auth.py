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

import os
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

# ── Microsoft (Azure AD, "common" multi-tenant endpoint) ───────────────────
# Uses the same authorization-code exchange pattern as GitHub/LinkedIn below
# (no MSAL.js dependency needed on the frontend — just a redirect + code).
MICROSOFT_CLIENT_ID     = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_REDIRECT_URI  = os.environ.get("MICROSOFT_REDIRECT_URI", "")
MICROSOFT_TOKEN_URL     = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_GRAPH_ME_URL         = "https://graph.microsoft.com/v1.0/me"

# ── GitHub ───────────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID     = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_TOKEN_URL     = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL      = "https://api.github.com/user"
GITHUB_EMAILS_URL    = "https://api.github.com/user/emails"

# ── LinkedIn (OpenID Connect) ───────────────────────────────────────────────
LINKEDIN_CLIENT_ID     = os.environ.get("LINKEDIN_CLIENT_ID", "")
LINKEDIN_CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
LINKEDIN_REDIRECT_URI  = os.environ.get("LINKEDIN_REDIRECT_URI", "")
LINKEDIN_TOKEN_URL     = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL  = "https://api.linkedin.com/v2/userinfo"


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


def _exchange_microsoft_code(code: str) -> str:
    """Exchange a Microsoft OAuth `code` for an access_token."""
    if not (MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and MICROSOFT_REDIRECT_URI):
        raise HTTPException(500, detail="Microsoft sign-in is not configured on the server.")
    try:
        resp = _requests.post(
            MICROSOFT_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  MICROSOFT_REDIRECT_URI,
                "client_id":     MICROSOFT_CLIENT_ID,
                "client_secret": MICROSOFT_CLIENT_SECRET,
                "scope":         "User.Read openid email profile",
            },
            timeout=10,
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise HTTPException(401, detail=f"Microsoft code exchange failed: {data.get('error_description', 'unknown error')}")
        return token
    except HTTPException:
        raise
    except Exception as e:
        log.error("Microsoft code exchange failed: %s", e)
        raise HTTPException(401, detail=f"Could not exchange Microsoft code: {e}")


def _fetch_microsoft_profile(access_token: str) -> dict:
    """Fetch a Microsoft/Azure AD profile from Graph using an access_token."""
    try:
        resp = _requests.get(
            MS_GRAPH_ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(401, detail="Invalid Microsoft access token")
        data = resp.json()
        email = data.get("mail") or data.get("userPrincipalName")
        if not email:
            raise HTTPException(401, detail="Microsoft profile missing email")
        return {
            "email":    email,
            "sub":      data.get("id", ""),
            "name":     data.get("displayName", ""),
            "picture":  "",  # Graph photo requires a separate binary call — skip for now
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("Microsoft token verification failed: %s", e)
        raise HTTPException(401, detail=f"Could not verify Microsoft token: {e}")


def _exchange_github_code(code: str) -> str:
    """Exchange a GitHub OAuth `code` for an access_token."""
    if not (GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET):
        raise HTTPException(500, detail="GitHub sign-in is not configured on the server.")
    try:
        resp = _requests.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id":     GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code":          code,
            },
            timeout=10,
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise HTTPException(401, detail=f"GitHub code exchange failed: {data.get('error_description', 'unknown error')}")
        return token
    except HTTPException:
        raise
    except Exception as e:
        log.error("GitHub code exchange failed: %s", e)
        raise HTTPException(401, detail=f"Could not exchange GitHub code: {e}")


def _fetch_github_profile(access_token: str) -> dict:
    """Fetch profile + verified primary email from GitHub using an access_token."""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
    try:
        user_resp = _requests.get(GITHUB_USER_URL, headers=headers, timeout=10)
        if user_resp.status_code != 200:
            raise HTTPException(401, detail="Invalid GitHub access token")
        user = user_resp.json()

        email = user.get("email")
        if not email:
            # Primary email is often private — fetch it explicitly.
            emails_resp = _requests.get(GITHUB_EMAILS_URL, headers=headers, timeout=10)
            if emails_resp.status_code == 200:
                for e in emails_resp.json():
                    if e.get("primary") and e.get("verified"):
                        email = e.get("email")
                        break
        if not email:
            raise HTTPException(401, detail="GitHub account has no verified public email. Please make an email public or verify one.")

        return {
            "email":   email,
            "sub":     str(user.get("id", "")),
            "name":    user.get("name") or user.get("login", ""),
            "picture": user.get("avatar_url", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("GitHub profile fetch failed: %s", e)
        raise HTTPException(401, detail=f"Could not fetch GitHub profile: {e}")


def _exchange_linkedin_code(code: str) -> str:
    """Exchange a LinkedIn OAuth `code` for an access_token."""
    if not (LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET and LINKEDIN_REDIRECT_URI):
        raise HTTPException(500, detail="LinkedIn sign-in is not configured on the server.")
    try:
        resp = _requests.post(
            LINKEDIN_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  LINKEDIN_REDIRECT_URI,
                "client_id":     LINKEDIN_CLIENT_ID,
                "client_secret": LINKEDIN_CLIENT_SECRET,
            },
            timeout=10,
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise HTTPException(401, detail=f"LinkedIn code exchange failed: {data.get('error_description', 'unknown error')}")
        return token
    except HTTPException:
        raise
    except Exception as e:
        log.error("LinkedIn code exchange failed: %s", e)
        raise HTTPException(401, detail=f"Could not exchange LinkedIn code: {e}")


def _fetch_linkedin_profile(access_token: str) -> dict:
    """Fetch profile via LinkedIn's OpenID Connect /userinfo endpoint."""
    try:
        resp = _requests.get(
            LINKEDIN_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(401, detail="Invalid LinkedIn access token")
        data = resp.json()
        email = data.get("email")
        if not email:
            raise HTTPException(401, detail="LinkedIn profile missing email")
        return {
            "email":   email,
            "sub":     data.get("sub", ""),
            "name":    data.get("name", ""),
            "picture": data.get("picture", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error("LinkedIn profile fetch failed: %s", e)
        raise HTTPException(401, detail=f"Could not fetch LinkedIn profile: {e}")


def _get_or_create_oauth_user(provider: str, email: str, provider_uid: str, name: str, picture: str, email_verified: bool = True) -> dict:
    """
    Provider-agnostic version of _get_or_create_google_user — looks up a user
    by email, creates one if it doesn't exist. Used by Google, Microsoft,
    GitHub, and LinkedIn sign-in so each provider only needs to fetch its own
    profile info and hand it off here.
    """
    email = email.lower().strip()

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
        uid = user_doc["uid"] if "uid" in user_doc else user_doc.get("id")

        try:
            db.collection("users").document(uid).update({
                f"{provider}_uid":  provider_uid,
                "last_login":       _now_iso(),
                "auth_provider":    provider,
                "email_verified":   user_doc.get("email_verified", False) or email_verified,
            })
        except Exception as e:
            log.warning("Failed to update %s fields for %s: %s", provider, uid, e)

        user_doc.update({f"{provider}_uid": provider_uid, "last_login": _now_iso()})
        return user_doc

    # ── New user — create account ──────────────────────────────────────────
    uid = str(uuid.uuid4())
    new_user = {
        "uid":                  uid,
        "id":                   uid,   # auth_router.py's user docs key off "id"; keep both in sync
        "email":                email,
        "full_name":            name,
        "display_name":         name,
        "avatar_url":           picture,
        f"{provider}_uid":      provider_uid,
        "email_verified":       email_verified,
        "auth_provider":        provider,
        "role":                 "user",
        "profession":           "",
        "bio":                  "",
        "interests":            [],
        "subscribe_email":      True,
        "subscribed_newsletter": True,   # opt-in by default on OAuth signup
        "created_at":           _now_iso(),
        "last_login":           _now_iso(),
    }
    try:
        db.collection("users").document(uid).set(new_user)
        db.collection("newsletter_subscribers").document(uid).set({
            "uid":        uid,
            "email":      email,
            "name":       name,
            "subscribed": True,
            "source":     f"{provider}_signup",
            "created_at": _now_iso(),
        })
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to create user: {e}")

    log.info("New %s user created: uid=%s email=%s", provider, uid, email)
    return new_user


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

    return _get_or_create_oauth_user("google", email, google_uid, name, picture, email_verified)


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST BODIES
# ══════════════════════════════════════════════════════════════════════════════

class GoogleSignInBody(BaseModel):
    """Body for POST /api/auth/google"""
    id_token: str


class MicrosoftSignInBody(BaseModel):
    """Body for POST /api/auth/microsoft — the `code` from Microsoft's OAuth redirect"""
    code: str


class GithubSignInBody(BaseModel):
    """Body for POST /api/auth/github — the `code` from GitHub's OAuth redirect"""
    code: str


class LinkedinSignInBody(BaseModel):
    """Body for POST /api/auth/linkedin — the `code` from LinkedIn's OAuth redirect"""
    code: str


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
    token = create_jwt(user_id=user["uid"], role=user.get("role", "user"))

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


def _oauth_response(user: dict, is_new: bool) -> dict:
    """Shared response shape for every OAuth provider — same as POST /api/auth/login."""
    token = create_jwt(user_id=user["uid"], role=user.get("role", "user"))
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


def _is_new_email(email: str) -> bool:
    return not bool(
        list(db.collection("users").where("email", "==", email.lower()).limit(1).stream())
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/auth/microsoft  — Sign in or sign up with Microsoft (Azure AD)
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/microsoft",
    summary="Sign in or sign up with a Microsoft access token — PUBLIC",
    tags=["Auth"],
)
def microsoft_sign_in(body: MicrosoftSignInBody):
    """
    **Public endpoint.** Sign in or create an account using Microsoft (Azure AD) OAuth.

    The frontend redirects to Microsoft's authorize URL, Microsoft redirects
    back with a `code` query param, and the frontend POSTs that code here.
    The backend exchanges it server-side for an access_token (requires
    MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET / MICROSOFT_REDIRECT_URI
    env vars) and reads the profile from Microsoft Graph.

    Body: `{ "code": "<code-from-microsoft-redirect>" }`
    Response: same shape as POST /api/auth/login.
    """
    access_token = _exchange_microsoft_code(body.code)
    profile = _fetch_microsoft_profile(access_token)
    is_new = _is_new_email(profile["email"])
    user = _get_or_create_oauth_user("microsoft", profile["email"], profile["sub"], profile["name"], profile["picture"])
    log.info("microsoft_sign_in: uid=%s email=%s new=%s", user["uid"], user["email"], is_new)
    return _oauth_response(user, is_new)


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/auth/github  — Sign in or sign up with GitHub
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/github",
    summary="Sign in or sign up with a GitHub OAuth code — PUBLIC",
    tags=["Auth"],
)
def github_sign_in(body: GithubSignInBody):
    """
    **Public endpoint.** Sign in or create an account using GitHub OAuth.

    The frontend redirects to GitHub's authorize URL, GitHub redirects back
    with a `code` query param, and the frontend POSTs that code here. The
    backend exchanges it server-side for an access_token (requires
    GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET env vars) and fetches the
    verified primary email.

    Body: `{ "code": "<code-from-github-redirect>" }`
    Response: same shape as POST /api/auth/login.
    """
    access_token = _exchange_github_code(body.code)
    profile = _fetch_github_profile(access_token)
    is_new = _is_new_email(profile["email"])
    user = _get_or_create_oauth_user("github", profile["email"], profile["sub"], profile["name"], profile["picture"])
    log.info("github_sign_in: uid=%s email=%s new=%s", user["uid"], user["email"], is_new)
    return _oauth_response(user, is_new)


# ══════════════════════════════════════════════════════════════════════════════
# POST /api/auth/linkedin  — Sign in or sign up with LinkedIn
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/linkedin",
    summary="Sign in or sign up with a LinkedIn OAuth code — PUBLIC",
    tags=["Auth"],
)
def linkedin_sign_in(body: LinkedinSignInBody):
    """
    **Public endpoint.** Sign in or create an account using LinkedIn OpenID Connect.

    The frontend redirects to LinkedIn's authorize URL (scopes: `openid profile
    email`), LinkedIn redirects back with a `code`, and the frontend POSTs
    that code here. The backend exchanges it for an access_token (requires
    LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET / LINKEDIN_REDIRECT_URI env
    vars) and reads the profile from LinkedIn's /userinfo endpoint.

    Body: `{ "code": "<code-from-linkedin-redirect>" }`
    Response: same shape as POST /api/auth/login.
    """
    access_token = _exchange_linkedin_code(body.code)
    profile = _fetch_linkedin_profile(access_token)
    is_new = _is_new_email(profile["email"])
    user = _get_or_create_oauth_user("linkedin", profile["email"], profile["sub"], profile["name"], profile["picture"])
    log.info("linkedin_sign_in: uid=%s email=%s new=%s", user["uid"], user["email"], is_new)
    return _oauth_response(user, is_new)


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