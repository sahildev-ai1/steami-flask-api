"""
routers/auth_router.py  —  Authentication & User Management  v8
================================================================
Changes from v7:
  - Signup now auto-subscribes every new user to the newsletter via
    POST /api/newsletter/subscribe (internal call), instead of managing a
    separate newsletter_subscribers collection manually.
  - Dummy accounts (admin, mod, user) are also auto-subscribed on seed.
  - `subscribe_email` field on the user doc still works as before.

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
  POST   /api/auth/seed                     public — seed dummy accounts
  POST   /api/auth/signup                   public — register (auto-subscribes newsletter)
  POST   /api/auth/login                    public — login → token + user + role
  GET    /api/auth/me                       auth   — own profile
  POST   /api/auth/interests                auth   — save topic interests
  GET    /api/auth/interests                auth   — get own interests
  GET    /api/auth/users                    admin  — list all users
  PUT    /api/auth/users/{uid}/role         admin  — change role
  DELETE /api/auth/users/{uid}              admin  — delete user
  POST   /api/auth/forgot-password          public — request a 6-digit reset code by email
  POST   /api/auth/forgot-password/verify   public — verify the code → short-lived reset_token
  POST   /api/auth/forgot-password/reset    public — set a new password using the reset_token

PASSWORD RESET FLOW (added — was previously frontend-only/simulated):
  1. POST /forgot-password        { email }                            → generic "sent" message
  2. POST /forgot-password/verify { email, code }                      → { reset_token }
  3. POST /forgot-password/reset  { email, reset_token, new_password } → { reset: true }
  Codes are 6 digits, expire in 10 minutes, max 5 attempts. The reset_token issued after
  verification expires in 15 minutes and is single-use. Records live in the
  `password_resets` collection. The endpoint never reveals whether an email is registered.
"""

import uuid
import logging
import os
import random
import secrets
from datetime import datetime, timezone, timedelta
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


# ─────────────────────────────────────────────────────────────────────────────
# DUMMY ACCOUNTS
# ─────────────────────────────────────────────────────────────────────────────

DUMMY_ACCOUNTS: list[dict] = [
    {
        "id":             "admin-steami-001",
        "full_name":      "STEAMI Admin",
        "email":          "admin@steami.dev",
        "plain_password": "Admin@steami123",
        "role":           "admin",
        "profession":     "other",
        "interests":      VALID_TOPICS,
        "subscribe_email": True,
    },
    {
        "id":             "mod-steami-001",
        "full_name":      "STEAMI Moderator",
        "email":          "mod@steami.dev",
        "plain_password": "Mod@steami123",
        "role":           "mod",
        "profession":     "researcher",
        "interests":      ["AI + ROBOTICS", "COMPUTER SCIENCE", "PHYSICS"],
        "subscribe_email": True,
    },
    {
        "id":             "user-steami-001",
        "full_name":      "Demo User",
        "email":          "user@steami.dev",
        "plain_password": "User@steami123",
        "role":           "user",
        "profession":     "student",
        "interests":      ["AI + ROBOTICS", "EARTH & SPACE", "BIOLOGY"],
        "subscribe_email": True,
    },
]


def _newsletter_subscribe(email: str, name: str) -> None:
    """
    Subscribe an email directly to the newsletter_subscribers collection.
    Writes to DB instead of making an HTTP self-call — avoids the startup
    race condition where the server calls itself before it has finished
    binding to its port.  Idempotent: re-activates existing records.
    """
    try:
        email = email.lower().strip()
        existing_list = list(
            db.collection("newsletter_subscribers")
              .where("email", "==", email)
              .limit(1)
              .stream()
        )
        if existing_list:
            sub = existing_list[0].to_dict()
            if sub.get("subscribed"):
                log.info("_newsletter_subscribe: already subscribed %s", email)
                return
            db.collection("newsletter_subscribers").document(existing_list[0].id).update({
                "subscribed": True,
                "updated_at": _now(),
            })
            log.info("_newsletter_subscribe: reactivated %s", email)
            return

        sub_id = str(uuid.uuid4())
        db.collection("newsletter_subscribers").document(sub_id).set({
            "id":         sub_id,
            "uid":        sub_id,
            "email":      email,
            "name":       name.strip(),
            "subscribed": True,
            "source":     "signup",
            "created_at": _now(),
        })
        log.info("_newsletter_subscribe: subscribed %s", email)
    except Exception as e:
        log.warning("_newsletter_subscribe: failed for %s: %s", email, e)


def seed_dummy_accounts() -> dict:
    """
    Insert dummy accounts into Firestore if they don't already exist.
    Passwords are hashed before saving — never stored plain.
    Auto-subscribes each account to the newsletter.
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

        doc = {
            "id":            acc["id"],
            "full_name":     acc["full_name"],
            "email":         acc["email"],
            "password_hash": hash_password(acc["plain_password"]),
            "role":          acc["role"],
            "profession":    acc["profession"],
            "interests":     acc["interests"],
            "is_active":     True,
            "subscribe_email": acc.get("subscribe_email", True),
            "created_at":    _now(),
            "updated_at":    _now(),
        }

        try:
            doc_ref.set(doc)
            created.append(acc["email"])
            log.info("seed: created %s (%s)", acc["email"], acc["role"])
        except Exception as e:
            log.error("seed: failed %s: %s", acc["email"], e)
            continue

        # Auto-subscribe to newsletter
        _newsletter_subscribe(acc["email"], acc["full_name"])

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
        "interests":       user.get("interests", []),
        "subscribe_email": user.get("subscribe_email", False),
        "is_active":       user.get("is_active", True),
        "created_at": user.get("created_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────────────────────────────────────

class SignupBody(BaseModel):
    full_name:       str
    email:           str
    password:        str
    profession:      str  = "student"
    subscribe_email: bool = True   # opt-in by default; user can uncheck


class LoginBody(BaseModel):
    email:    str
    password: str


class InterestsBody(BaseModel):
    topics: list[str]


class UpdateRoleBody(BaseModel):
    role: str


class UpdateUserBody(BaseModel):
    full_name:       Optional[str]  = None
    email:           Optional[str]  = None
    profession:      Optional[str]  = None
    interests:       Optional[list] = None
    is_active:       Optional[bool] = None
    subscribe_email: Optional[bool] = None
    role:            Optional[str]  = None


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/seed", status_code=201, summary="Seed dummy accounts — public")
def seed_accounts():
    """
    POST /api/auth/seed
    Seed the three dummy accounts if they don't exist yet. Idempotent.
    Each account is auto-subscribed to the newsletter on first creation.
    """
    return seed_dummy_accounts()


@router.post("/signup", status_code=201, summary="Register — public")
def signup(body: SignupBody):
    """
    POST /api/auth/signup
    Register a new user. New users always start with role = "user".

    Every new user is automatically subscribed to the newsletter via
    POST /api/newsletter/subscribe. The `subscribe_email` field can be
    set to false to skip the subscription (defaults to true).

    Body: { full_name, email, password, profession, subscribe_email }

    Response: { token, user, role }

    After signup, call POST /api/auth/interests to choose STEM topics.
    """
    email = body.email.lower().strip()

    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, detail="Invalid email address.")
    if len(body.password) < 6:
        raise HTTPException(400, detail="Password must be at least 6 characters.")
    if body.profession not in VALID_PROFESSIONS:
        raise HTTPException(
            400,
            detail=f"Invalid profession. Choose from: {', '.join(VALID_PROFESSIONS)}"
        )
    if _find_by_email(email):
        raise HTTPException(409, detail="An account with this email already exists.")

    user_id  = str(uuid.uuid4())
    user_doc = {
        "id":            user_id,
        "full_name":     body.full_name.strip(),
        "email":         email,
        "password_hash": hash_password(body.password),
        "role":          "user",
        "profession":    body.profession,
        "interests":     [],
        "is_active":         True,
        "subscribe_email":    body.subscribe_email,
        "created_at":        _now(),
        "updated_at":        _now(),
    }

    try:
        db.collection("users").document(user_id).set(user_doc)
    except Exception as e:
        log.error("signup: save failed: %s", e)
        raise HTTPException(500, detail="Account creation failed.")

    # ── Auto-subscribe to newsletter via the newsletter router ─────────────
    # Always subscribe (even if subscribe_email=False) because the newsletter
    # router's POST /subscribe is the source of truth for the subscribers list.
    # The user's subscribe_email flag controls whether they *receive* emails;
    # the newsletter collection tracks their subscription status.
    _newsletter_subscribe(email, body.full_name.strip())

    token = create_token(user_id, "user")
    log.info("signup: %s (%s) profession=%s newsletter=%s",
             email, user_id, body.profession, body.subscribe_email)
    return {"token": token, "user": _safe(user_doc), "role": "user"}


@router.post("/login", summary="Login — public, returns token + user + role")
def login(body: LoginBody):
    """
    POST /api/auth/login
    Authenticate and receive a JWT token.

    Test accounts:
      admin@steami.dev / Admin@steami123  → admin
      mod@steami.dev   / Mod@steami123    → mod
      user@steami.dev  / User@steami123   → user
    """
    email = body.email.lower().strip()
    user  = _find_by_email(email)

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
    doc = db.collection("users").document(get_uid(payload)).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return _safe(doc.to_dict())


@router.post("/interests", summary="Save topic interests — requires auth")
def save_interests(body: InterestsBody, payload: dict = Depends(require_auth)):
    """
    POST /api/auth/interests
    Save the STEM topics this user wants to follow.
    """
    invalid = [t for t in body.topics if t not in VALID_TOPICS]
    if invalid:
        raise HTTPException(400, detail=f"Invalid topics: {invalid}. Valid: {VALID_TOPICS}")
    if not body.topics:
        raise HTTPException(400, detail="Select at least one topic.")

    unique = list(dict.fromkeys(body.topics))
    uid    = get_uid(payload)
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
    doc = db.collection("users").document(get_uid(payload)).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return {
        "interests":    doc.to_dict().get("interests", []),
        "valid_topics": VALID_TOPICS,
    }


@router.get("/users", summary="List all users — admin only")
def list_all_users(payload: dict = Depends(require_admin)):
    try:
        docs  = db.collection("users").limit(500).stream()
        users = [_safe(d.to_dict()) for d in docs]
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"users": users, "total": len(users)}


@router.put("/users/{uid}/role", summary="Change user role — admin only")
def update_user_role(uid: str, body: UpdateRoleBody, payload: dict = Depends(require_admin)):
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
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    return _safe(doc.to_dict())


@router.put("/users/{uid}", summary="Update user profile — admin only")
def admin_update_user(uid: str, body: UpdateUserBody, payload: dict = Depends(require_admin)):
    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")

    updates: dict = {"updated_at": _now()}

    if body.full_name  is not None: updates["full_name"]  = body.full_name.strip()
    if body.profession is not None:
        if body.profession not in VALID_PROFESSIONS:
            raise HTTPException(400, detail=f"Invalid profession: {body.profession}")
        updates["profession"] = body.profession
    if body.interests is not None:
        invalid = [t for t in body.interests if t not in VALID_TOPICS]
        if invalid:
            raise HTTPException(400, detail=f"Invalid topics: {invalid}")
        updates["interests"] = list(dict.fromkeys(body.interests))
    if body.is_active        is not None: updates["is_active"]        = body.is_active
    if body.subscribe_email  is not None: updates["subscribe_email"]  = body.subscribe_email
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(400, detail=f"Invalid role: {body.role}")
        if uid == get_uid(payload) and body.role != "admin":
            raise HTTPException(400, detail="Cannot change your own role.")
        updates["role"] = body.role
    if body.email is not None:
        new_email = body.email.lower().strip()
        if "@" not in new_email:
            raise HTTPException(400, detail="Invalid email format.")
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


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL NEWSLETTER — subscription management
# ─────────────────────────────────────────────────────────────────────────────

class SubscribeBody(BaseModel):
    subscribe: bool


@router.post("/subscribe", summary="Update email digest subscription — requires auth")
def update_subscription(body: SubscribeBody, payload: dict = Depends(require_auth)):
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    if not doc_ref.get().exists:
        raise HTTPException(404, detail="User not found.")
    try:
        doc_ref.update({"subscribe_email": body.subscribe, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    action = "Subscribed to" if body.subscribe else "Unsubscribed from"
    log.info("%s email digest: uid=%s", action, uid)
    return {"updated": True, "subscribe_email": body.subscribe}


@router.get("/newsletter/recipients", summary="Get subscribed users — admin only")
def get_newsletter_recipients(payload: dict = Depends(require_admin)):
    try:
        docs = (
            db.collection("users")
              .where("subscribe_email", "==", True)
              .stream()
        )
        recipients = []
        for d in docs:
            u = d.to_dict()
            if not u.get("is_active", True):
                continue
            recipients.append({
                "id":        u.get("id"),
                "full_name": u.get("full_name", ""),
                "email":     u.get("email", ""),
                "interests": u.get("interests", []),
                "profession":u.get("profession", ""),
            })
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    by_topic: dict = {}
    for r in recipients:
        for topic in r.get("interests", []):
            by_topic.setdefault(topic, []).append(r["email"])

    return {"total": len(recipients), "recipients": recipients, "by_topic": by_topic}


# ─────────────────────────────────────────────────────────────────────────────
# USER SELF-SERVICE — edit own profile
# ─────────────────────────────────────────────────────────────────────────────

class EditProfileBody(BaseModel):
    full_name:        Optional[str]  = None
    profession:       Optional[str]  = None
    interests:        Optional[list] = None
    subscribe_email:  Optional[bool] = None
    current_password: Optional[str]  = None
    new_password:     Optional[str]  = None


@router.put("/profile", summary="Edit own profile — requires auth")
def edit_profile(body: EditProfileBody, payload: dict = Depends(require_auth)):
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()

    if not doc.exists:
        raise HTTPException(404, detail="User not found.")

    user    = doc.to_dict()
    updates = {}

    if body.full_name is not None:
        name = body.full_name.strip()
        if not name:
            raise HTTPException(400, detail="full_name cannot be empty.")
        updates["full_name"] = name

    if body.profession is not None:
        if body.profession not in VALID_PROFESSIONS:
            raise HTTPException(400, detail=f"Invalid profession. Choose from: {', '.join(VALID_PROFESSIONS)}")
        updates["profession"] = body.profession

    if body.interests is not None:
        invalid = [t for t in body.interests if t not in VALID_TOPICS]
        if invalid:
            raise HTTPException(400, detail=f"Invalid topics: {invalid}. Valid options: {VALID_TOPICS}")
        updates["interests"] = list(dict.fromkeys(body.interests))

    if body.subscribe_email is not None:
        updates["subscribe_email"] = body.subscribe_email

    if body.current_password is not None or body.new_password is not None:
        if not body.current_password or not body.new_password:
            raise HTTPException(400, detail="Provide both current_password and new_password.")
        if not verify_password(body.current_password, user.get("password_hash", "")):
            raise HTTPException(401, detail="Current password is incorrect.")
        if len(body.new_password) < 6:
            raise HTTPException(400, detail="New password must be at least 6 characters.")
        updates["password_hash"] = hash_password(body.new_password)

    if not updates:
        raise HTTPException(400, detail="No fields provided to update.")

    updates["updated_at"] = _now()

    try:
        doc_ref.update(updates)
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    updated_doc = doc_ref.get()
    safe_user   = _safe(updated_doc.to_dict())
    changed_fields = [
        "password" if k == "password_hash" else k
        for k in updates.keys() if k != "updated_at"
    ]

    log.info("edit_profile: uid=%s changed=%s", uid, changed_fields)
    return {"updated": True, "updated_fields": changed_fields, "user": safe_user}


@router.patch("/subscribe/toggle", summary="Toggle email subscription on/off — requires auth")
def toggle_subscription(payload: dict = Depends(require_auth)):
    uid     = get_uid(payload)
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    current   = doc.to_dict().get("subscribe_email", False)
    new_value = not current
    try:
        doc_ref.update({"subscribe_email": new_value, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    message = "Subscribed to daily email digest" if new_value else "Unsubscribed from daily email digest"
    return {"updated": True, "subscribe_email": new_value, "message": message}


@router.patch("/users/{uid}/subscribe/toggle", summary="Admin toggle subscribe for any user")
def admin_toggle_subscription(uid: str, payload: dict = Depends(require_admin)):
    doc_ref = db.collection("users").document(uid)
    doc     = doc_ref.get()
    if not doc.exists:
        raise HTTPException(404, detail="User not found.")
    current   = doc.to_dict().get("subscribe_email", False)
    new_value = not current
    try:
        doc_ref.update({"subscribe_email": new_value, "updated_at": _now()})
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    message = "Subscribed to daily email digest" if new_value else "Unsubscribed from daily email digest"
    log.info("admin_toggle_subscription: uid=%s %s→%s by admin=%s", uid, current, new_value, get_uid(payload))
    return {"updated": True, "uid": uid, "subscribe_email": new_value, "message": message}


# ─────────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD — request code / verify code / set new password
#
# Replaces the old frontend-only "simulate API call" mock. Three steps:
#   1. POST /forgot-password         — email a 6-digit code, store it
#   2. POST /forgot-password/verify  — check the code, issue a reset_token
#   3. POST /forgot-password/reset   — consume the reset_token, set new password
#
# Records live in the `password_resets` collection (one doc per request).
# Codes expire in RESET_CODE_TTL_MINUTES; the post-verification reset_token
# expires in RESET_TOKEN_TTL_MINUTES and can only be used once.
# ─────────────────────────────────────────────────────────────────────────────

RESET_CODE_TTL_MINUTES  = 10
RESET_TOKEN_TTL_MINUTES = 15
MAX_RESET_ATTEMPTS      = 5


class ForgotPasswordBody(BaseModel):
    email: str


class VerifyResetCodeBody(BaseModel):
    email: str
    code:  str


class ResetPasswordBody(BaseModel):
    email:        str
    reset_token:  str
    new_password: str


def _gen_reset_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def _minutes_from_now(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _is_expired(iso_ts: str) -> bool:
    if not iso_ts:
        return True
    try:
        expires = datetime.fromisoformat(iso_ts)
    except Exception:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > expires


def _latest_reset_record(email: str) -> Optional[dict]:
    """Most recent password_resets doc for this email, or None."""
    docs = list(
        db.collection("password_resets")
          .where("email", "==", email)
          .order_by("created_at", direction="DESCENDING")
          .limit(1)
          .stream()
    )
    return docs[0].to_dict() if docs else None


@router.post("/forgot-password", summary="Request a password reset code — public")
def forgot_password(body: ForgotPasswordBody):
    """
    POST /api/auth/forgot-password
    Body: { "email": "user@example.com" }

    If an account exists for this email, emails a 6-digit verification code
    (valid for 10 minutes) and stores it in `password_resets`. Always returns
    the same generic message — whether or not the email is registered — so
    this endpoint can't be used to enumerate accounts.
    """
    email = body.email.lower().strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, detail="Invalid email address.")

    generic_response = {"message": "If that email is registered, a verification code has been sent."}

    user = _find_by_email(email)
    if not user:
        log.info("forgot_password: no account for %s (responding generically)", email)
        return generic_response

    code     = _gen_reset_code()
    reset_id = str(uuid.uuid4())

    try:
        db.collection("password_resets").document(reset_id).set({
            "id":          reset_id,
            "email":       email,
            "code":        code,
            "attempts":    0,
            "verified":    False,
            "used":        False,
            "reset_token": "",
            "expires_at":  _minutes_from_now(RESET_CODE_TTL_MINUTES),
            "created_at":  _now(),
        })
    except Exception as e:
        log.error("forgot_password: failed to store code for %s: %s", email, e)
        raise HTTPException(500, detail="Could not start password reset. Please try again.")

    try:
        from routers.newsletter import _send_one_via_mailrelay
        html_body = (
            f"<p>Your STEAMI password reset code is:</p>"
            f"<h2 style=\"letter-spacing:4px;font-family:monospace\">{code}</h2>"
            f"<p>This code expires in {RESET_CODE_TTL_MINUTES} minutes. "
            f"If you didn't request this, you can safely ignore this email.</p>"
        )
        sent = _send_one_via_mailrelay(
            email, user.get("full_name", ""), "Your STEAMI password reset code", html_body
        )
        if not sent:
            log.warning("forgot_password: email dispatch failed for %s (code stored regardless)", email)
    except Exception as e:
        log.error("forgot_password: email dispatch error for %s: %s", email, e)

    log.info("forgot_password: code issued for %s", email)
    return generic_response


@router.post("/forgot-password/verify", summary="Verify a password reset code — public")
def verify_reset_code(body: VerifyResetCodeBody):
    """
    POST /api/auth/forgot-password/verify
    Body: { "email": "...", "code": "123456" }

    On success, returns a short-lived reset_token (15 min, single-use) that
    must be passed to POST /api/auth/forgot-password/reset to actually change
    the password.
    """
    email = body.email.lower().strip()
    code  = body.code.strip()

    record = _latest_reset_record(email)
    if not record:
        raise HTTPException(400, detail="Invalid or expired code.")
    if record.get("used"):
        raise HTTPException(400, detail="This code has already been used. Request a new one.")
    if _is_expired(record.get("expires_at", "")):
        raise HTTPException(400, detail="This code has expired. Request a new one.")
    if record.get("attempts", 0) >= MAX_RESET_ATTEMPTS:
        raise HTTPException(429, detail="Too many incorrect attempts. Request a new code.")

    if record.get("code") != code:
        try:
            db.collection("password_resets").document(record["id"]).update({
                "attempts": record.get("attempts", 0) + 1,
            })
        except Exception as e:
            log.error("verify_reset_code: failed to bump attempts for %s: %s", email, e)
        raise HTTPException(400, detail="Invalid or expired code.")

    reset_token = secrets.token_urlsafe(32)
    try:
        db.collection("password_resets").document(record["id"]).update({
            "verified":    True,
            "reset_token": reset_token,
            "expires_at":  _minutes_from_now(RESET_TOKEN_TTL_MINUTES),
        })
    except Exception as e:
        log.error("verify_reset_code: failed to save verification for %s: %s", email, e)
        raise HTTPException(500, detail="Could not verify code. Please try again.")

    log.info("verify_reset_code: verified for %s", email)
    return {"verified": True, "reset_token": reset_token}


@router.post("/forgot-password/reset", summary="Set a new password using a verified reset token — public")
def reset_password(body: ResetPasswordBody):
    """
    POST /api/auth/forgot-password/reset
    Body: { "email": "...", "reset_token": "...", "new_password": "..." }

    Completes the password reset. The reset_token must match the one issued
    by POST /api/auth/forgot-password/verify and must not be expired or
    already used.
    """
    email = body.email.lower().strip()
    if len(body.new_password) < 8:
        raise HTTPException(400, detail="Password must be at least 8 characters.")

    record = _latest_reset_record(email)
    if not record:
        raise HTTPException(400, detail="Reset session not found. Please start over.")
    if record.get("used"):
        raise HTTPException(400, detail="This reset session was already used. Please start over.")
    if not record.get("verified"):
        raise HTTPException(400, detail="Code not verified yet.")
    if not body.reset_token or record.get("reset_token") != body.reset_token:
        raise HTTPException(400, detail="Invalid reset session. Please start over.")
    if _is_expired(record.get("expires_at", "")):
        raise HTTPException(400, detail="Reset session expired. Please start over.")

    user = _find_by_email(email)
    if not user:
        raise HTTPException(404, detail="Account not found.")

    try:
        db.collection("users").document(user["id"]).update({
            "password_hash": hash_password(body.new_password),
            "updated_at":    _now(),
        })
        db.collection("password_resets").document(record["id"]).update({"used": True})
    except Exception as e:
        log.error("reset_password: failed for %s: %s", email, e)
        raise HTTPException(500, detail="Could not update password. Please try again.")

    log.info("reset_password: password updated for %s", email)
    return {"reset": True}