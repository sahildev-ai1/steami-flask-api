"""
auth.py — Authentication utilities for STEAMI API
====================================================
Implements:
  - Password hashing using PBKDF2-HMAC-SHA256 (stdlib hashlib — no bcrypt needed)
  - JWT creation and verification using HMAC-SHA256 (stdlib hmac — no python-jose needed)
  - Role-based access control: admin | mod | user
  - FastAPI dependency functions for protected routes

JWT format used here:
  Header.Payload.Signature  (base64url encoded, same as standard JWT)
  Payload fields: sub (user_id), role, exp (unix timestamp)

THREE ROLES:
  admin  — full access: manage users, promote/demote mods, seed data, all APIs
  mod    — elevated access: can manage articles/content, cannot manage users
  user   — normal access: can use insight API, chat, feed; cannot manage anything

PUBLIC routes (no token needed):
  GET  /health
  POST /api/auth/signup
  POST /api/auth/login
  GET  /api/articles
  GET  /api/articles/{id}
  GET  /api/articles/fetch
  GET  /api/explainers
  GET  /api/explainers/{id}
  GET  /api/research/articles
  GET  /api/research/articles/{id}
  GET  /api/research/fields
  GET  /api/feed/items
  GET  /api/sources
  POST /api/feed/from-selection  ← public so anonymous users can use selection feed

PROTECTED routes (token required):
  POST /api/articles/{id}/insight    ← requires: user | mod | admin
  GET  /api/insights                 ← requires: user | mod | admin
  GET  /api/insights/{id}            ← requires: user | mod | admin
  ALL  /api/chat/*                   ← requires: user | mod | admin
  POST /api/articles/fetch           ← requires: mod | admin
  POST /api/articles/fetch-source    ← requires: mod | admin
  POST /api/articles/{id}/insight    ← requires: user | mod | admin
  DELETE /api/articles/{id}/insight  ← requires: mod | admin
  POST /api/explainers/seed          ← requires: admin
  POST /api/research/seed            ← requires: admin
  POST /api/explainers               ← requires: admin | mod
  PUT  /api/explainers/{id}          ← requires: admin | mod
  DELETE /api/explainers/{id}        ← requires: admin
  POST /api/research/articles        ← requires: admin | mod
  PUT  /api/research/articles/{id}   ← requires: admin | mod
  DELETE /api/research/articles/{id} ← requires: admin
  GET  /api/auth/users               ← requires: admin
  PUT  /api/auth/users/{uid}/role    ← requires: admin
  DELETE /api/auth/users/{uid}       ← requires: admin
"""

import os
import hmac
import hashlib
import base64
import json
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Secret key for signing JWTs.
# In production: set JWT_SECRET in your .env file to a long random string.
JWT_SECRET: str = os.environ.get("JWT_SECRET", "steami-super-secret-key-change-in-production")

# Token validity period in seconds (7 days default)
TOKEN_EXPIRY_SECONDS: int = int(os.environ.get("TOKEN_EXPIRY_SECONDS", str(7 * 24 * 3600)))

# Valid roles — order matters for permission checks
ROLES = ["user", "mod", "admin"]


# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD HASHING  (PBKDF2-HMAC-SHA256 via stdlib hashlib)
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    """
    Hash a plain-text password using PBKDF2-HMAC-SHA256.
    Returns a string in the format:  salt$hash
    Both parts are hex-encoded. Salt is 32 random bytes.
    """
    # Generate a fresh random 32-byte salt
    salt = os.urandom(32)
    # Derive key: 260,000 iterations is OWASP recommended minimum for PBKDF2-SHA256
    key = hashlib.pbkdf2_hmac(
        hash_name   = "sha256",
        password    = plain.encode("utf-8"),
        salt        = salt,
        iterations  = 260_000,
        dklen       = 32,    # 32-byte output
    )
    # Return as "salt$hash" — both parts hex-encoded for safe storage
    return salt.hex() + "$" + key.hex()


def verify_password(plain: str, stored: str) -> bool:
    """
    Verify a plain-text password against a stored PBKDF2 hash.
    stored format must be:  salt_hex$hash_hex
    Returns True if the password matches, False otherwise.
    """
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        key  = hashlib.pbkdf2_hmac(
            hash_name  = "sha256",
            password   = plain.encode("utf-8"),
            salt       = salt,
            iterations = 260_000,
            dklen      = 32,
        )
        # hmac.compare_digest prevents timing attacks
        return hmac.compare_digest(key.hex(), hash_hex)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# JWT  (HS256 using stdlib hmac + hashlib, no python-jose needed)
# ─────────────────────────────────────────────────────────────────────────────

def _b64url_encode(data: bytes) -> str:
    """Base64-URL encode without padding — standard JWT encoding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(s: str) -> bytes:
    """Base64-URL decode — adds padding back before decoding."""
    # Add missing padding
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(user_id: str, role: str) -> str:
    """
    Create a signed JWT token.

    Payload contains:
      sub  — user ID (subject)
      role — user role ("user" | "mod" | "admin")
      iat  — issued-at timestamp (Unix seconds)
      exp  — expiry timestamp (Unix seconds)

    Returns the full token string:  header.payload.signature
    """
    # Standard JWT header — HS256 algorithm
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())

    # Build payload
    now = int(time.time())
    payload = _b64url_encode(json.dumps({
        "sub":  user_id,
        "role": role,
        "iat":  now,
        "exp":  now + TOKEN_EXPIRY_SECONDS,
    }).encode())

    # Sign: HMAC-SHA256 over "header.payload"
    signing_input = f"{header}.{payload}".encode("utf-8")
    sig = hmac.new(
        key     = JWT_SECRET.encode("utf-8"),
        msg     = signing_input,
        digestmod = hashlib.sha256,
    ).digest()
    signature = _b64url_encode(sig)

    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> dict:
    """
    Decode and verify a JWT token.
    Raises ValueError with a descriptive message on any failure.
    Returns the payload dict on success: { sub, role, iat, exp }
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token — expected 3 parts")

        header_b64, payload_b64, sig_b64 = parts

        # Re-compute expected signature
        signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig  = hmac.new(
            key       = JWT_SECRET.encode("utf-8"),
            msg       = signing_input,
            digestmod = hashlib.sha256,
        ).digest()
        expected_b64  = _b64url_encode(expected_sig)

        # Constant-time comparison prevents timing attacks
        if not hmac.compare_digest(sig_b64, expected_b64):
            raise ValueError("Invalid signature")

        # Decode payload
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))

        # Check expiry
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token has expired")

        return payload

    except ValueError:
        raise  # re-raise our own errors
    except Exception as e:
        raise ValueError(f"Token decode failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI SECURITY SCHEME
# ─────────────────────────────────────────────────────────────────────────────

# HTTPBearer extracts the token from the Authorization: Bearer <token> header
_bearer = HTTPBearer(auto_error=False)


def _get_token_payload(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[dict]:
    """
    Internal helper — extracts and decodes the bearer token from the request.
    Returns the payload dict, or None if no token was provided.
    Raises HTTP 401 if a token was provided but is invalid.
    """
    if credentials is None:
        return None  # No token provided — caller decides if that's OK
    try:
        return decode_token(credentials.credentials)
    except ValueError as e:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = f"Invalid or expired token: {e}",
            headers     = {"WWW-Authenticate": "Bearer"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC DEPENDENCY — extracts token if present but does NOT require it
# ─────────────────────────────────────────────────────────────────────────────

def maybe_user(payload: Optional[dict] = Depends(_get_token_payload)) -> Optional[dict]:
    """
    Dependency for routes that are PUBLIC but can also use the user info
    if a token is provided (e.g. personalised responses).
    Use: current_user: Optional[dict] = Depends(maybe_user)
    """
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# PROTECTED DEPENDENCIES — require a valid token with the right role
# ─────────────────────────────────────────────────────────────────────────────

def require_auth(payload: Optional[dict] = Depends(_get_token_payload)) -> dict:
    """
    Require any authenticated user (user | mod | admin).
    Use for: chat, insight generation, personal feed.
    Raises HTTP 401 if no valid token.
    """
    if payload is None:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Authentication required. Please log in.",
            headers     = {"WWW-Authenticate": "Bearer"},
        )
    return payload


def require_mod(payload: dict = Depends(require_auth)) -> dict:
    """
    Require mod or admin role.
    Use for: content management (articles, explainers, research).
    Raises HTTP 403 if role is insufficient.
    """
    if payload.get("role") not in ("mod", "admin"):
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Moderator or admin access required.",
        )
    return payload


def require_admin(payload: dict = Depends(require_auth)) -> dict:
    """
    Require admin role only.
    Use for: user management, seeding data, deleting content.
    Raises HTTP 403 if role is not admin.
    """
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Admin access required.",
        )
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY — extract user ID from token payload
# ─────────────────────────────────────────────────────────────────────────────

def get_uid(payload: dict) -> str:
    """Extract the user ID from a decoded token payload."""
    return payload.get("sub", "")