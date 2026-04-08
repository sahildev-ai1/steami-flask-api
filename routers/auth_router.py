"""
routers/auth_router.py — Authentication & User Management
===========================================================
Handles signup, login, role management and user CRUD.

DUMMY ACCOUNTS seeded on startup (also created by POST /api/auth/seed):
  ┌──────────────────────────────────────────────────────────────────┐
  │  ADMIN                                                           │
  │    Email:    admin@steami.dev                                    │
  │    Password: Admin@steami123                                     │
  │    Role:     admin                                               │
  ├──────────────────────────────────────────────────────────────────┤
  │  MOD                                                             │
  │    Email:    mod@steami.dev                                      │
  │    Password: Mod@steami123                                       │
  │    Role:     mod                                                 │
  ├──────────────────────────────────────────────────────────────────┤
  │  USER                                                            │
  │    Email:    user@steami.dev                                     │
  │    Password: User@steami123                                      │
  │    Role:     user                                                │
  └──────────────────────────────────────────────────────────────────┘

ENDPOINTS:
  POST /api/auth/seed              — seed dummy accounts (public, idempotent)
  POST /api/auth/signup            — register new user (public)
  POST /api/auth/login             — login, returns token + user info (public)
  GET  /api/auth/me                — get own profile (requires auth)
  GET  /api/auth/users             — list all users (requires admin)
  PUT  /api/auth/users/{uid}/role  — change a user's role (requires admin)
  DELETE /api/auth/users/{uid}     — delete a user (requires admin)

Firestore collection: `users`
Document fields:
  id, full_name, email, password_hash, role, domain_of_interest,
  background, statement_of_purpose, created_at, updated_at, is_active
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr

from firestore_client import db
from auth import (
    hash_password,
    verify_password,
    create_token,
    require_auth,
    require_admin,
    get_uid,
    ROLES,
)

log = logging.getLogger(__name__)
router = APIRouter()


def _now() -> str:
    """Return current UTC time as ISO string."""
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# DUMMY ACCOUNTS
# These are seeded into Firestore on startup and via POST /api/auth/seed.
# Passwords are hashed at seed time — never stored in plain text.
# ─────────────────────────────────────────────────────────────────────────────

DUMMY_ACCOUNTS = [
    {
        # ADMIN — full platform access, can manage all users and content
        "id":                   "admin-steami-001",
        "full_name":            "STEAMI Admin",
        "email":                "admin@steami.dev",
        "plain_password":       "Admin@steami123",    # ← shown once here, hashed before saving
        "role":                 "admin",
        "domain_of_interest":   "Platform Administration",
        "background":           "STEAMI platform administrator with full system access.",
        "statement_of_purpose": "Managing the STEAMI platform and its users.",
    },
    {
        # MOD — content management access, upgraded normal user
        "id":                   "mod-steami-001",
        "full_name":            "STEAMI Moderator",
        "email":                "mod@steami.dev",
        "plain_password":       "Mod@steami123",
        "role":                 "mod",
        "domain_of_interest":   "Science Communication, AI",
        "background":           "Experienced science communicator and content curator.",
        "statement_of_purpose": "Curating high-quality STEM content for the STEAMI community.",
    },
    {
        # USER — normal registered user
        "id":                   "user-steami-001",
        "full_name":            "Demo User",
        "email":                "user@steami.dev",
        "plain_password":       "User@steami123",
        "role":                 "user",
        "domain_of_interest":   "AI, Space, Biology/Medicine",
        "background":           "Curious student interested in science and technology.",
        "statement_of_purpose": "Exploring cutting-edge STEM research and news.",
    },
]


def seed_dummy_accounts() -> dict:
    """
    Create the three dummy accounts in Firestore if they don't exist yet.
    Uses merge=True so re-running never overwrites an existing account.
    Called automatically on app startup.
    Returns a summary of what was created/skipped.
    """
    created  = []
    skipped  = []

    for acc in DUMMY_ACCOUNTS:
        doc_ref = db.collection("users").document(acc["id"])
        existing = doc_ref.get()

        if existing.exists:
            # Account already in Firestore — don't touch it
            skipped.append(acc["email"])
            log.info("seed_dummy_accounts: skipped existing %s (%s)", acc["email"], acc["role"])
            continue

        # Hash the plain password before saving
        doc = {
            "id":                   acc["id"],
            "full_name":            acc["full_name"],
            "email":                acc["email"],
            "password_hash":        hash_password(acc["plain_password"]),  # ← hashed
            "role":                 acc["role"],
            "domain_of_interest":   acc["domain_of_interest"],
            "background":           acc["background"],
            "statement_of_purpose": acc["statement_of_purpose"],
            "is_active":            True,
            "created_at":           _now(),
            "updated_at":           _now(),
        }

        try:
            doc_ref.set(doc)
            created.append(acc["email"])
            log.info("seed_dummy_accounts: created %s (%s)", acc["email"], acc["role"])
        except Exception as e:
            log.error("seed_dummy_accounts: failed to create %s: %s", acc["email"], e)

    return {"created": created, "skipped": skipped}


# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS — request / response shapes
# ─────────────────────────────────────────────────────────────────────────────

class SignupBody(BaseModel):
    """
    Registration form fields — matches the signup form in the screenshot exactly.
    full_name, email, domain_of_interest, background, statement_of_purpose
    """
    full_name:            str            # "Your full name" field
    email:                str            # "your@email.com" field
    password:             str            # Not shown in form but required for auth
    domain_of_interest:   str   = ""     # "e.g. Climate Research, Systems Thinking..."
    background:           str   = ""     # "Briefly describe your academic or professional background..."
    statement_of_purpose: str   = ""     # "Why do you want to join this program?..."


class LoginBody(BaseModel):
    """Login credentials."""
    email:    str
    password: str


class UpdateRoleBody(BaseModel):
    """Admin-only: change a user's role."""
    role: str  # must be one of ROLES: "user" | "mod" | "admin"


# ─────────────────────────────────────────────────────────────────────────────
# HELPER — look up a user by email
# ─────────────────────────────────────────────────────────────────────────────

def _find_user_by_email(email: str) -> Optional[dict]:
    """
    Search the `users` collection for a document with matching email.
    Returns the user dict or None if not found.
    Firestore REST API doesn't support direct field queries on all plans,
    so we use a structured query via _Query.where().
    """
    try:
        docs = (
            db.collection("users")
              .where("email", "==", email.lower().strip())
              .limit(1)
              .stream()
        )
        for d in docs:
            return d.to_dict()
        return None
    except Exception as e:
        log.error("_find_user_by_email failed: %s", e)
        return None


def _safe_user(user: dict) -> dict:
    """
    Return a user dict safe to send to the frontend.
    Strips the password_hash — never return this to clients.
    """
    return {
        "id":                   user.get("id"),
        "full_name":            user.get("full_name"),
        "email":                user.get("email"),
        "role":                 user.get("role"),
        "domain_of_interest":   user.get("domain_of_interest", ""),
        "background":           user.get("background", ""),
        "statement_of_purpose": user.get("statement_of_purpose", ""),
        "is_active":            user.get("is_active", True),
        "created_at":           user.get("created_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/seed", status_code=201)
def seed_accounts():
    """
    POST /api/auth/seed
    Seed the three dummy accounts (admin, mod, user) into Firestore.
    Safe to call multiple times — existing accounts are never overwritten.
    PUBLIC endpoint — call once after first deploy.

    Response:
    {
      "created": ["admin@steami.dev"],  // newly created
      "skipped": ["mod@steami.dev", "user@steami.dev"]  // already existed
    }

    curl -X POST http://127.0.0.1:5000/api/auth/seed
    """
    result = seed_dummy_accounts()
    return result


@router.post("/signup", status_code=201)
def signup(body: SignupBody):
    """
    POST /api/auth/signup
    Register a new user. All fields match the signup form in the UI.
    New users always get role = "user". Admins can upgrade them later.

    Body:
    {
      "full_name":            "Sahil Kumar",
      "email":                "sahil@example.com",
      "password":             "SecurePass123",
      "domain_of_interest":   "AI, Robotics",
      "background":           "CS student at Delhi University",
      "statement_of_purpose": "Want to explore cutting-edge STEM research"
    }

    Response:
    {
      "token": "eyJ...",
      "user":  { id, full_name, email, role: "user", ... },
      "role":  "user"
    }

    curl -X POST http://127.0.0.1:5000/api/auth/signup \\
      -H "Content-Type: application/json" \\
      -d '{"full_name":"Test User","email":"test@example.com","password":"Test@123"}'
    """
    # Normalise email to lowercase
    email = body.email.lower().strip()

    # Validate email format (basic check)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, detail="Invalid email address format.")

    # Validate password length
    if len(body.password) < 6:
        raise HTTPException(400, detail="Password must be at least 6 characters.")

    # Check if email already registered
    existing = _find_user_by_email(email)
    if existing:
        raise HTTPException(409, detail="An account with this email already exists.")

    # Build user document
    user_id = str(uuid.uuid4())
    user_doc = {
        "id":                   user_id,
        "full_name":            body.full_name.strip(),
        "email":                email,
        "password_hash":        hash_password(body.password),   # ← always hash
        "role":                 "user",                          # ← new users always start as "user"
        "domain_of_interest":   body.domain_of_interest.strip(),
        "background":           body.background.strip(),
        "statement_of_purpose": body.statement_of_purpose.strip(),
        "is_active":            True,
        "created_at":           _now(),
        "updated_at":           _now(),
    }

    # Save to Firestore
    try:
        db.collection("users").document(user_id).set(user_doc)
    except Exception as e:
        log.error("signup: Firestore save failed: %s", e)
        raise HTTPException(500, detail="Account creation failed. Please try again.")

    # Issue JWT token immediately — user is logged in right after signup
    token = create_token(user_id, "user")

    log.info("signup: new user %s (%s)", email, user_id)
    return {
        "token": token,
        "user":  _safe_user(user_doc),
        "role":  "user",
    }


@router.post("/login")
def login(body: LoginBody):
    """
    POST /api/auth/login
    Authenticate with email + password. Returns JWT token, user info, and role.

    Body:
    {
      "email":    "admin@steami.dev",
      "password": "Admin@steami123"
    }

    Response:
    {
      "token": "eyJ...",
      "user": {
        "id":                   "admin-steami-001",
        "full_name":            "STEAMI Admin",
        "email":                "admin@steami.dev",
        "role":                 "admin",
        "domain_of_interest":   "Platform Administration",
        "background":           "...",
        "statement_of_purpose": "...",
        "is_active":            true,
        "created_at":           "..."
      },
      "role": "admin"
    }

    Test accounts:
      admin@steami.dev  / Admin@steami123  → role: admin
      mod@steami.dev    / Mod@steami123    → role: mod
      user@steami.dev   / User@steami123   → role: user

    curl -X POST http://127.0.0.1:5000/api/auth/login \\
      -H "Content-Type: application/json" \\
      -d '{"email":"admin@steami.dev","password":"Admin@steami123"}'
    """
    email = body.email.lower().strip()

    # Find user by email
    user = _find_user_by_email(email)
    if not user:
        # Use generic message to prevent email enumeration attacks
        raise HTTPException(401, detail="Invalid email or password.")

    # Check account is active
    if not user.get("is_active", True):
        raise HTTPException(403, detail="Account has been deactivated. Contact admin.")

    # Verify password
    if not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(401, detail="Invalid email or password.")

    # Issue token
    token = create_token(user["id"], user["role"])

    log.info("login: %s (%s) role=%s", email, user["id"], user["role"])
    return {
        "token": token,
        "user":  _safe_user(user),
        "role":  user["role"],
    }


@router.get("/me")
def get_me(payload: dict = Depends(require_auth)):
    """
    GET /api/auth/me
    Get the currently logged-in user's profile.
    Requires: any valid token (user | mod | admin)

    Response: user object without password_hash

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/auth/me
    """
    user_id = get_uid(payload)
    doc = db.collection("users").document(user_id).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return _safe_user(doc.to_dict())


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN-ONLY — user management
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/users")
def list_all_users(
    payload: dict = Depends(require_admin),  # ← ADMIN ONLY
):
    """
    GET /api/auth/users
    List all registered users. ADMIN ONLY.

    Response:
    {
      "users": [ { id, full_name, email, role, is_active, created_at }, ... ],
      "total": 15
    }

    curl -H "Authorization: Bearer <admin_token>" http://127.0.0.1:5000/api/auth/users
    """
    try:
        docs = db.collection("users").limit(500).stream()
        users = [_safe_user(d.to_dict()) for d in docs]
    except Exception as e:
        log.error("list_all_users failed: %s", e)
        raise HTTPException(500, detail=str(e))
    return {"users": users, "total": len(users)}


@router.put("/users/{uid}/role")
def update_user_role(
    uid:     str,
    body:    UpdateRoleBody,
    payload: dict = Depends(require_admin),  # ← ADMIN ONLY
):
    """
    PUT /api/auth/users/{uid}/role
    Change a user's role. ADMIN ONLY.
    Use this to promote a user to mod, or demote a mod back to user.

    Body: { "role": "mod" }   // "user" | "mod" | "admin"

    Response: { "updated": true, "uid": "...", "new_role": "mod" }

    curl -X PUT http://127.0.0.1:5000/api/auth/users/USER_ID/role \\
      -H "Authorization: Bearer <admin_token>" \\
      -H "Content-Type: application/json" \\
      -d '{"role":"mod"}'
    """
    # Validate the requested role
    if body.role not in ROLES:
        raise HTTPException(400, detail=f"Invalid role. Must be one of: {', '.join(ROLES)}")

    # Prevent admin from demoting themselves
    if uid == get_uid(payload) and body.role != "admin":
        raise HTTPException(400, detail="Admins cannot change their own role.")

    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")

    try:
        doc_ref.update({"role": body.role, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("update_user_role: uid=%s new_role=%s by admin=%s", uid, body.role, get_uid(payload))
    return {"updated": True, "uid": uid, "new_role": body.role}


@router.delete("/users/{uid}")
def delete_user(
    uid:     str,
    payload: dict = Depends(require_admin),  # ← ADMIN ONLY
):
    """
    DELETE /api/auth/users/{uid}
    Permanently delete a user account. ADMIN ONLY.
    Cannot delete your own account (safety guard).

    Response: { "deleted": true, "uid": "..." }

    curl -X DELETE http://127.0.0.1:5000/api/auth/users/USER_ID \\
      -H "Authorization: Bearer <admin_token>"
    """
    # Prevent admin from deleting themselves
    if uid == get_uid(payload):
        raise HTTPException(400, detail="You cannot delete your own account.")

    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")

    try:
        doc_ref.delete()
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("delete_user: uid=%s by admin=%s", uid, get_uid(payload))
    return {"deleted": True, "uid": uid}