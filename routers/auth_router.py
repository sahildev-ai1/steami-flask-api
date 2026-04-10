"""
routers/auth_router.py  —  Authentication & User Management  v7
================================================================
Changes from v6:
  - Signup: replaced domain_of_interest/background/statement_of_purpose
    with a single `profession` field (student/professional/professor/etc.)
  - New POST /api/auth/interests  — save user's STEM topic interests
  - New GET  /api/auth/interests  — get current user's interests

PROFESSION OPTIONS:
  student | working_professional | professor |
  researcher | self_learner | educator | other

INTEREST TOPICS (10 canonical STEM domains shown to user after signup):
  PHYSICS | CHEMISTRY | BIOLOGY | MEDICINE | EARTH & SPACE |
  COMPUTER SCIENCE | AI + ROBOTICS | ENGINEERING |
  MATHEMATICS & DATA | CLIMATE & ENERGY

DUMMY ACCOUNTS (seeded on startup):
  admin@steami.dev   /  Admin@steami123   — role: admin
  mod@steami.dev     /  Mod@steami123     — role: mod
  user@steami.dev    /  User@steami123    — role: user

ALL ENDPOINTS:
  POST   /api/auth/seed                 public — seed dummy accounts
  POST   /api/auth/signup               public — register
  POST   /api/auth/login                public — login → token + user + role
  GET    /api/auth/me                   auth   — own profile
  POST   /api/auth/interests            auth   — save topic interests
  GET    /api/auth/interests            auth   — get own interests
  GET    /api/auth/users                admin  — list all users
  PUT    /api/auth/users/{uid}/role     admin  — change role
  DELETE /api/auth/users/{uid}          admin  — delete user
"""

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from mongodb_client import db
from auth import (
    hash_password, verify_password, create_token,
    require_auth, require_admin, get_uid, ROLES,
)

log    = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Profession options shown in the signup form
VALID_PROFESSIONS: list[str] = [
    "student",              # school / college / university student
    "working_professional", # employed in industry
    "professor",            # university / college faculty
    "researcher",           # academic or industrial researcher
    "self_learner",         # independent learner / autodidact
    "educator",             # school-level teacher
    "other",                # anything else
]

# The 10 STEM interest topics shown in the post-signup onboarding screen.
# These must match the topic keys used in article_fetcher.DOMAIN_KEYWORDS.
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


# ─────────────────────────────────────────────────────────────────────────────
# DUMMY ACCOUNTS  — plain passwords shown here only, always hashed before save
# ─────────────────────────────────────────────────────────────────────────────

DUMMY_ACCOUNTS: list[dict] = [
    {
        "id":             "admin-steami-001",
        "full_name":      "STEAMI Admin",
        "email":          "admin@steami.dev",
        "plain_password": "Admin@steami123",
        "role":           "admin",
        "profession":     "other",
        "interests":      VALID_TOPICS,       # admin tracks all topics
    },
    {
        "id":             "mod-steami-001",
        "full_name":      "STEAMI Moderator",
        "email":          "mod@steami.dev",
        "plain_password": "Mod@steami123",
        "role":           "mod",
        "profession":     "researcher",
        "interests":      ["AI + ROBOTICS", "COMPUTER SCIENCE", "PHYSICS"],
    },
    {
        "id":             "user-steami-001",
        "full_name":      "Demo User",
        "email":          "user@steami.dev",
        "plain_password": "User@steami123",
        "role":           "user",
        "profession":     "student",
        "interests":      ["AI + ROBOTICS", "EARTH & SPACE", "BIOLOGY"],
    },
]


def seed_dummy_accounts() -> dict:
    """
    Insert dummy accounts into Firestore if they don't already exist.
    Passwords are hashed before saving — never stored plain.
    Called automatically by the startup event in main.py.
    """
    created: list[str] = []
    skipped: list[str] = []

    for acc in DUMMY_ACCOUNTS:
        doc_ref  = db.collection("users").document(acc["id"])
        existing = doc_ref.get()

        if existing.exists:
            skipped.append(acc["email"])
            continue

        # Build the Firestore document — hash the password
        doc = {
            "id":            acc["id"],
            "full_name":     acc["full_name"],
            "email":         acc["email"],
            "password_hash": hash_password(acc["plain_password"]),
            "role":          acc["role"],
            "profession":    acc["profession"],
            "interests":     acc["interests"],
            "is_active":     True,
            "created_at":    _now(),
            "updated_at":    _now(),
        }

        try:
            doc_ref.set(doc)
            created.append(acc["email"])
            log.info("seed: created %s (%s)", acc["email"], acc["role"])
        except Exception as e:
            log.error("seed: failed %s: %s", acc["email"], e)

    return {"created": created, "skipped": skipped}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_by_email(email: str) -> Optional[dict]:
    """Query Firestore for a user with the given email. Returns dict or None."""
    try:
        docs = (
            db.collection("users")
              .where("email", "==", email.lower().strip())
              .limit(1).stream()
        )
        for d in docs:
            return d.to_dict()
        return None
    except Exception as e:
        log.error("_find_by_email(%s): %s", email, e)
        return None


def _safe(user: dict) -> dict:
    """Strip password_hash — never send it to the frontend."""
    return {
        "id":         user.get("id"),
        "full_name":  user.get("full_name"),
        "email":      user.get("email"),
        "role":       user.get("role"),
        "profession": user.get("profession", ""),
        "interests":  user.get("interests", []),
        "is_active":  user.get("is_active", True),
        "created_at": user.get("created_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class SignupBody(BaseModel):
    """
    Signup form — 4 fields only.
    profession must be one of VALID_PROFESSIONS.
    """
    full_name:  str
    email:      str
    password:   str
    profession: str = "student"


class LoginBody(BaseModel):
    email:    str
    password: str


class InterestsBody(BaseModel):
    """List of topic strings from VALID_TOPICS."""
    topics: list[str]


class UpdateRoleBody(BaseModel):
    role: str  # "user" | "mod" | "admin"


class UpdateUserBody(BaseModel):
    """
    Admin: update any field on a user profile.
    All fields are optional — only provided fields are changed.
    To deactivate an account set is_active=False.
    """
    full_name:  Optional[str]       = None
    email:      Optional[str]       = None
    profession: Optional[str]       = None
    interests:  Optional[list]      = None
    is_active:  Optional[bool]      = None
    role:       Optional[str]       = None   # can also change role here


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/seed", status_code=201, summary="Seed dummy accounts — public")
def seed_accounts():
    """
    POST /api/auth/seed
    Seed the three dummy accounts if they don't exist yet. Idempotent.

    curl -X POST http://127.0.0.1:5000/api/auth/seed
    """
    return seed_dummy_accounts()


@router.post("/signup", status_code=201, summary="Register — public")
def signup(body: SignupBody):
    """
    POST /api/auth/signup
    Register a new user. New users always start with role = "user".

    Body: { full_name, email, password, profession }

    Profession options:
      student | working_professional | professor |
      researcher | self_learner | educator | other

    Response: { token, user, role }

    After signup, call POST /api/auth/interests to choose STEM topics.

    curl -X POST http://127.0.0.1:5000/api/auth/signup \\
      -H "Content-Type: application/json" \\
      -d '{"full_name":"Sahil","email":"s@e.com","password":"Test@123","profession":"student"}'
    """
    email = body.email.lower().strip()

    # Validate email format
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, detail="Invalid email address.")

    # Validate password length
    if len(body.password) < 6:
        raise HTTPException(400, detail="Password must be at least 6 characters.")

    # Validate profession
    if body.profession not in VALID_PROFESSIONS:
        raise HTTPException(
            400,
            detail=f"Invalid profession. Choose from: {', '.join(VALID_PROFESSIONS)}"
        )

    # Check for duplicate email
    if _find_by_email(email):
        raise HTTPException(409, detail="An account with this email already exists.")

    # Build and save user document
    user_id  = str(uuid.uuid4())
    user_doc = {
        "id":            user_id,
        "full_name":     body.full_name.strip(),
        "email":         email,
        "password_hash": hash_password(body.password),   # hash immediately
        "role":          "user",                          # always "user" at signup
        "profession":    body.profession,
        "interests":     [],                              # set later via /interests
        "is_active":     True,
        "created_at":    _now(),
        "updated_at":    _now(),
    }

    try:
        db.collection("users").document(user_id).set(user_doc)
    except Exception as e:
        log.error("signup: save failed: %s", e)
        raise HTTPException(500, detail="Account creation failed.")

    token = create_token(user_id, "user")
    log.info("signup: %s (%s) profession=%s", email, user_id, body.profession)
    return {"token": token, "user": _safe(user_doc), "role": "user"}


@router.post("/login", summary="Login — public, returns token + user + role")
def login(body: LoginBody):
    """
    POST /api/auth/login
    Authenticate and receive a JWT token.

    Body: { email, password }

    Test accounts:
      admin@steami.dev / Admin@steami123  → admin
      mod@steami.dev   / Mod@steami123    → mod
      user@steami.dev  / User@steami123   → user

    Response: { token, user: { id, full_name, email, role, profession, interests }, role }

    curl -X POST http://127.0.0.1:5000/api/auth/login \\
      -H "Content-Type: application/json" \\
      -d '{"email":"admin@steami.dev","password":"Admin@steami123"}'
    """
    email = body.email.lower().strip()
    user  = _find_by_email(email)

    # Generic error message prevents email enumeration
    if not user:
        raise HTTPException(401, detail="Invalid email or password.")

    if not user.get("is_active", True):
        raise HTTPException(403, detail="Account deactivated. Contact admin.")

    if not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(401, detail="Invalid email or password.")

    token = create_token(user["id"], user["role"])
    log.info("login: %s role=%s", email, user["role"])
    return {"token": token, "user": _safe(user), "role": user["role"]}


@router.get("/me", summary="Get own profile — requires auth")
def get_me(payload: dict = Depends(require_auth)):
    """
    GET /api/auth/me
    Returns the currently authenticated user's profile.

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/auth/me
    """
    doc = db.collection("users").document(get_uid(payload)).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return _safe(doc.to_dict())


@router.post("/interests", summary="Save topic interests — requires auth")
def save_interests(
    body:    InterestsBody,
    payload: dict = Depends(require_auth),  # any logged-in user
):
    """
    POST /api/auth/interests
    Save the STEM topics this user wants to follow.
    Called during post-signup onboarding or whenever the user updates prefs.

    topics must be a non-empty subset of the 10 valid STEM topics:
      PHYSICS | CHEMISTRY | BIOLOGY | MEDICINE | EARTH & SPACE |
      COMPUTER SCIENCE | AI + ROBOTICS | ENGINEERING |
      MATHEMATICS & DATA | CLIMATE & ENERGY

    Body: { "topics": ["AI + ROBOTICS", "PHYSICS", "EARTH & SPACE"] }

    Response: { updated, interests, valid_topics }

    curl -X POST http://127.0.0.1:5000/api/auth/interests \\
      -H "Authorization: Bearer <token>" \\
      -H "Content-Type: application/json" \\
      -d '{"topics":["AI + ROBOTICS","PHYSICS"]}'
    """
    # Validate every topic
    invalid = [t for t in body.topics if t not in VALID_TOPICS]
    if invalid:
        raise HTTPException(
            400,
            detail=f"Invalid topics: {invalid}. Valid: {VALID_TOPICS}"
        )

    if not body.topics:
        raise HTTPException(400, detail="Select at least one topic.")

    # Deduplicate preserving order
    unique = list(dict.fromkeys(body.topics))

    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")

    try:
        doc_ref.update({"interests": unique, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("interests saved: uid=%s topics=%s", uid, unique)
    return {"updated": True, "interests": unique, "valid_topics": VALID_TOPICS}


@router.get("/interests", summary="Get own interests — requires auth")
def get_interests(payload: dict = Depends(require_auth)):
    """
    GET /api/auth/interests
    Get the current user's saved STEM topic interests.

    Response: { interests: [...], valid_topics: [...all 10...] }

    curl -H "Authorization: Bearer <token>" http://127.0.0.1:5000/api/auth/interests
    """
    doc = db.collection("users").document(get_uid(payload)).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return {
        "interests":    doc.to_dict().get("interests", []),
        "valid_topics": VALID_TOPICS,
    }


@router.get("/users", summary="List all users — admin only")
def list_all_users(payload: dict = Depends(require_admin)):
    """
    GET /api/auth/users
    List every registered user. ADMIN ONLY.

    curl -H "Authorization: Bearer <admin_token>" http://127.0.0.1:5000/api/auth/users
    """
    try:
        docs  = db.collection("users").limit(500).stream()
        users = [_safe(d.to_dict()) for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"users": users, "total": len(users)}


@router.put("/users/{uid}/role", summary="Change user role — admin only")
def update_user_role(
    uid:     str,
    body:    UpdateRoleBody,
    payload: dict = Depends(require_admin),
):
    """
    PUT /api/auth/users/{uid}/role
    Promote or demote a user. ADMIN ONLY.
    Body: { "role": "mod" }

    curl -X PUT http://127.0.0.1:5000/api/auth/users/UID/role \\
      -H "Authorization: Bearer <admin_token>" \\
      -d '{"role":"mod"}'
    """
    if body.role not in ROLES:
        raise HTTPException(400, detail=f"Role must be: {', '.join(ROLES)}")
    if uid == get_uid(payload) and body.role != "admin":
        raise HTTPException(400, detail="Cannot change your own role.")
    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")
    doc_ref.update({"role": body.role, "updated_at": _now()})
    log.info("role changed: uid=%s → %s by %s", uid, body.role, get_uid(payload))
    return {"updated": True, "uid": uid, "new_role": body.role}


@router.delete("/users/{uid}", summary="Delete user — admin only")
def delete_user(uid: str, payload: dict = Depends(require_admin)):
    """
    DELETE /api/auth/users/{uid}
    Permanently delete a user account. ADMIN ONLY.

    curl -X DELETE http://127.0.0.1:5000/api/auth/users/UID \\
      -H "Authorization: Bearer <admin_token>"
    """
    if uid == get_uid(payload):
        raise HTTPException(400, detail="Cannot delete your own account.")
    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")
    doc_ref.delete()
    log.info("deleted user: %s by %s", uid, get_uid(payload))
    return {"deleted": True, "uid": uid}

@router.get("/users/{uid}", summary="Get single user by ID — admin only")
def get_user_by_id(uid: str, payload: dict = Depends(require_admin)):
    """
    GET /api/auth/users/{uid}
    Get a single user's full profile by their ID. ADMIN ONLY.

    Response: full user object (without password_hash)

    curl -H "Authorization: Bearer <admin_token>" http://127.0.0.1:5000/api/auth/users/USER_ID
    """
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return _safe(doc.to_dict())


@router.put("/users/{uid}", summary="Update user profile — admin only")
def admin_update_user(
    uid:     str,
    body:    UpdateUserBody,
    payload: dict = Depends(require_admin),
):
    """
    PUT /api/auth/users/{uid}
    Update any profile field for a user. ADMIN ONLY.
    Only the fields provided in the body are changed; others stay the same.

    Body (all fields optional):
    {
      "full_name":  "New Name",
      "email":      "new@email.com",
      "profession": "researcher",
      "interests":  ["PHYSICS", "AI + ROBOTICS"],
      "is_active":  true,
      "role":       "mod"
    }

    Response: { "updated": true, "uid": "..." }

    curl -X PUT http://127.0.0.1:5000/api/auth/users/USER_ID \
      -H "Authorization: Bearer <admin_token>" \
      -H "Content-Type: application/json" \
      -d '{"full_name":"Updated Name","is_active":true,"profession":"researcher"}'
    """
    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")

    # Build update dict with only fields that were provided (not None)
    updates: dict = {"updated_at": _now()}

    if body.full_name  is not None: updates["full_name"]  = body.full_name.strip()
    if body.profession is not None:
        if body.profession not in VALID_PROFESSIONS:
            raise HTTPException(400, detail=f"Invalid profession: {body.profession}")
        updates["profession"] = body.profession
    if body.interests  is not None:
        # Validate topic names
        invalid = [t for t in body.interests if t not in VALID_TOPICS]
        if invalid:
            raise HTTPException(400, detail=f"Invalid topics: {invalid}")
        updates["interests"] = list(dict.fromkeys(body.interests))
    if body.is_active  is not None: updates["is_active"]  = body.is_active
    if body.role       is not None:
        if body.role not in ROLES:
            raise HTTPException(400, detail=f"Invalid role: {body.role}")
        # Prevent admin from changing their own role
        if uid == get_uid(payload) and body.role != "admin":
            raise HTTPException(400, detail="Cannot change your own role.")
        updates["role"] = body.role
    if body.email is not None:
        new_email = body.email.lower().strip()
        if "@" not in new_email:
            raise HTTPException(400, detail="Invalid email format.")
        # Check the new email is not already taken by another user
        existing = _find_by_email(new_email)
        if existing and existing.get("id") != uid:
            raise HTTPException(409, detail="Email already in use by another account.")
        updates["email"] = new_email

    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    log.info("admin_update_user: uid=%s fields=%s by admin=%s",
             uid, list(updates.keys()), get_uid(payload))
    return {"updated": True, "uid": uid, "updated_fields": list(updates.keys())}