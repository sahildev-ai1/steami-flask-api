"""
visitor_tracker.py  —  Unique IP Visitor Tracking for STEAMI
=============================================================
Tracks every unique IP address that hits the backend.
If the request carries a valid JWT, looks up the user's name from the
'users' collection using the 'sub' (uid) from the token.
If not logged in (or token missing/invalid), stores "Unknown".

HOW IT WORKS:
  - A Starlette middleware intercepts every request AFTER DDoS protection.
  - It extracts the real client IP (same logic as ddos_protection.py).
  - It decodes the JWT to get the uid (sub field), then fetches the user's
    full_name from the users collection.
  - It upserts a document in the "visitors" collection keyed on IP.
  - Only ONE document per unique IP — updates name/last_seen on repeat visits.
  - Admin-only endpoints:
      GET    /api/visitors        — list all unique visitors (paginated)
      GET    /api/visitors/stats  — aggregated stats
      DELETE /api/visitors/{ip}   — remove a visitor record

COLLECTION SCHEMA (visitors):
  {
    "ip":          "1.2.3.4",
    "name":        "Sahil Tiwari",   # or "Unknown"
    "uid":         "abc123",
    "role":        "user",
    "first_seen":  "2025-04-01T10:00:00+00:00",
    "last_seen":   "2025-04-15T18:32:00+00:00",
    "visit_count": 42,
    "is_logged_in": true
  }

USAGE — add to main.py (AFTER add_ddos_protection):
  from visitor_tracker import add_visitor_tracking
  visitors_router = add_visitor_tracking(app)
  app.include_router(visitors_router, prefix="/api/visitors", tags=["Visitors"])
"""

import logging
import asyncio
import os
import hmac
import hashlib
import base64
import json
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_client_ip(request: Request) -> str:
    """
    Extract the real client IP.
    Mirrors ddos_protection.py so both systems agree on the IP.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    ip = ip.split(":")[0] if "." in ip else ip
    return ip or "unknown"


def _b64url_decode(s: str) -> bytes:
    """Base64-URL decode with padding fix — same as auth.py."""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _decode_jwt_soft(request: Request) -> Optional[dict]:
    """
    Decode the Bearer JWT using the SAME logic as auth.py (stdlib hmac/hashlib).
    Returns the payload dict {sub, role, iat, exp} on success, None on failure.
    Never raises.

    NOTE: Your JWT payload (from auth.py create_token) only contains:
      sub, role, iat, exp
    There is NO name field in the token — we must look up the name separately.
    """
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None

        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, sig_b64 = parts

        # Re-compute signature using the same JWT_SECRET as auth.py
        secret = os.environ.get("JWT_SECRET", "steami-super-secret-key-change-in-production")
        signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig  = hmac.new(
            key       = secret.encode("utf-8"),
            msg       = signing_input,
            digestmod = hashlib.sha256,
        ).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("utf-8")

        if not hmac.compare_digest(sig_b64, expected_b64):
            return None  # invalid signature

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))

        if payload.get("exp", 0) < int(time.time()):
            return None  # expired

        return payload

    except Exception:
        return None


def _lookup_user_name(uid: str) -> str:
    """
    Look up the user's display name from the 'users' collection by uid.
    Returns the name string, or "Unknown" if not found.

    Tries these fields in order: full_name, display_name, username, email
    """
    try:
        from mongodb_client import db
        doc = db.collection("users").document(uid).get()
        if doc.exists:
            data = doc.to_dict()
            name = (
                data.get("full_name")
                or data.get("display_name")
                or data.get("username")
                or data.get("name")
                or data.get("email")   # fallback to email if no name set
                or "Unknown"
            )
            return str(name).strip() or "Unknown"
    except Exception as e:
        log.debug("visitor_tracker: user lookup failed uid=%s: %s", uid, e)
    return "Unknown"


def _extract_user_info(request: Request) -> dict:
    """
    Decode the JWT → get uid → look up name from DB.
    Returns full identity dict, or guest defaults if no valid token.
    """
    payload = _decode_jwt_soft(request)

    if payload:
        uid  = payload.get("sub") or payload.get("uid") or ""
        role = payload.get("role", "user")

        # Look up real name from users collection
        # (JWT only has sub/role/iat/exp — no name field)
        name = _lookup_user_name(uid) if uid else "Unknown"

        return {
            "name":         name,
            "uid":          uid,
            "role":         role,
            "is_logged_in": True,
        }

    return {
        "name":         "Unknown",
        "uid":          None,
        "role":         None,
        "is_logged_in": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PATHS TO SKIP — health, static, docs (noise)
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_PREFIXES = (
    "/health",
    "/images/",
    "/static/",
    "/syswatch",
    "/.well-known",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/favicon",
    "/ai-context.txt",
)


# ─────────────────────────────────────────────────────────────────────────────
# MIDDLEWARE
# ─────────────────────────────────────────────────────────────────────────────

class VisitorTrackingMiddleware(BaseHTTPMiddleware):
    """
    Records unique visitor IPs after every successful request.
    Fire-and-forget — never blocks the response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return response

        ip   = _get_client_ip(request)
        user = _extract_user_info(request)

        asyncio.create_task(_upsert_visitor(ip, user))

        return response


async def _upsert_visitor(ip: str, user: dict) -> None:
    """
    Upsert visitor record in the 'visitors' collection.
    Uses db.collection() — matches the rest of STEAMI's DB access pattern.

    Rules:
      - last_seen and visit_count always updated.
      - Logged-in user → always upgrade name/uid/role.
      - Guest → set "Unknown" only on first insert, never overwrite a real name.
    """
    try:
        from mongodb_client import db

        now          = _now()
        existing_doc = db.collection("visitors").document(ip).get()

        if existing_doc.exists:
            existing = existing_doc.to_dict()

            updates: dict = {
                "last_seen":   now,
                "visit_count": existing.get("visit_count", 0) + 1,
            }

            # Upgrade to real name if logged in
            # Never downgrade a known name back to "Unknown"
            if user["is_logged_in"]:
                updates["name"]         = user["name"]
                updates["uid"]          = user["uid"]
                updates["role"]         = user["role"]
                updates["is_logged_in"] = True

            db.collection("visitors").document(ip).update(updates)

        else:
            # First visit — create the full document
            doc: dict = {
                "ip":           ip,
                "name":         user["name"],
                "uid":          user["uid"],
                "role":         user["role"],
                "is_logged_in": user["is_logged_in"],
                "first_seen":   now,
                "last_seen":    now,
                "visit_count":  1,
            }
            db.collection("visitors").document(ip).set(doc)

    except Exception as e:
        log.debug("visitor_tracker: upsert failed for %s: %s", ip, e)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────────────────────────────────────

def _build_router() -> APIRouter:
    from auth import require_admin

    r = APIRouter()

    # ── GET /api/visitors ────────────────────────────────────────────────────
    @r.get("", summary="List unique IP visitors — ADMIN ONLY")
    def list_visitors(
        limit:     int            = Query(100, ge=1, le=1000, description="Max records"),
        skip:      int            = Query(0,   ge=0,          description="Pagination offset"),
        logged_in: Optional[bool] = Query(None, description="true=logged-in only, false=guest only, omit=all"),
        _auth:     dict           = Depends(require_admin),
    ):
        from mongodb_client import db
        try:
            all_docs = db.collection("visitors").stream_all()
            visitors = [d.to_dict() for d in all_docs]

            if logged_in is True:
                visitors = [v for v in visitors if v.get("is_logged_in") is True]
            elif logged_in is False:
                visitors = [v for v in visitors if not v.get("is_logged_in")]

            visitors.sort(key=lambda v: v.get("last_seen", ""), reverse=True)

            total     = len(visitors)
            paginated = visitors[skip: skip + limit]

            return {
                "total":    total,
                "returned": len(paginated),
                "skip":     skip,
                "limit":    limit,
                "visitors": paginated,
            }
        except Exception as e:
            log.error("list_visitors: %s", e)
            raise HTTPException(500, detail=str(e))

    # ── GET /api/visitors/stats ──────────────────────────────────────────────
    @r.get("/stats", summary="Visitor stats summary — ADMIN ONLY")
    def visitor_stats(_auth: dict = Depends(require_admin)):
        from mongodb_client import db
        try:
            all_docs = db.collection("visitors").stream_all()
            visitors = [d.to_dict() for d in all_docs]

            total     = len(visitors)
            logged_in = sum(1 for v in visitors if v.get("is_logged_in"))
            unknown   = total - logged_in

            sorted_by_time = sorted(
                visitors, key=lambda v: v.get("last_seen", ""), reverse=True
            )
            latest_visit = sorted_by_time[0]["last_seen"] if sorted_by_time else None

            top_visitors = sorted(
                visitors, key=lambda v: v.get("visit_count", 0), reverse=True
            )[:5]
            top_visitors = [
                {
                    "ip":          v.get("ip"),
                    "name":        v.get("name"),
                    "visit_count": v.get("visit_count", 0),
                    "last_seen":   v.get("last_seen"),
                }
                for v in top_visitors
            ]

            return {
                "total_unique_ips": total,
                "logged_in":        logged_in,
                "unknown":          unknown,
                "latest_visit":     latest_visit,
                "top_visitors":     top_visitors,
            }
        except Exception as e:
            log.error("visitor_stats: %s", e)
            raise HTTPException(500, detail=str(e))

    # ── DELETE /api/visitors/{ip_addr} ───────────────────────────────────────
    @r.delete("/{ip_addr}", summary="Remove a visitor record — ADMIN ONLY")
    def delete_visitor(ip_addr: str, _auth: dict = Depends(require_admin)):
        from mongodb_client import db
        try:
            doc = db.collection("visitors").document(ip_addr).get()
            if not doc.exists:
                raise HTTPException(404, detail=f"Visitor {ip_addr} not found")
            db.collection("visitors").document(ip_addr).delete()
            log.info("visitor_tracker: deleted record for %s", ip_addr)
            return {"deleted": True, "ip": ip_addr}
        except HTTPException:
            raise
        except Exception as e:
            log.error("delete_visitor: %s", e)
            raise HTTPException(500, detail=str(e))

    return r


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def add_visitor_tracking(app) -> APIRouter:
    """
    Attach VisitorTrackingMiddleware and return the admin router.

    In main.py (AFTER add_ddos_protection):
        from visitor_tracker import add_visitor_tracking
        visitors_router = add_visitor_tracking(app)
        app.include_router(visitors_router, prefix="/api/visitors", tags=["Visitors"])
    """
    app.add_middleware(VisitorTrackingMiddleware)
    live_router = _build_router()
    log.info("Visitor tracking active — unique IPs recorded in 'visitors' collection")
    return live_router
