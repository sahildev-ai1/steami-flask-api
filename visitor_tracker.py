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
<<<<<<< HEAD
      GET    /api/visitors/stats  — aggregated stats (total, logged-in, unknown)
=======
      GET    /api/visitors/stats  — aggregated stats
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
      DELETE /api/visitors/{ip}   — remove a visitor record

COLLECTION SCHEMA (visitors):
  {
<<<<<<< HEAD
    "_id":         "<IP address>",   # unique key = IP
    "ip":          "1.2.3.4",
    "name":        "Sahil Tiwari",   # or "Unknown"
    "uid":         "abc123",         # user ID, or null
    "role":        "user",           # or null
=======
    "ip":          "1.2.3.4",
    "name":        "Sahil Tiwari",   # or "Unknown"
    "uid":         "abc123",
    "role":        "user",
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
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
<<<<<<< HEAD
=======
import hmac
import hashlib
import base64
import json
import time
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
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
<<<<<<< HEAD
    Checks X-Forwarded-For (nginx / Cloudflare) first, then direct host.
=======
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"
<<<<<<< HEAD

    # Strip port from IPv4 e.g. "1.2.3.4:5000" → "1.2.3.4"
=======
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
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
<<<<<<< HEAD
    Silently try to decode the Bearer JWT from the Authorization header.
    Returns payload dict on success, None on any failure.
    Never raises — used for optional enrichment only.
=======
    Decode the Bearer JWT using the SAME logic as auth.py (stdlib hmac/hashlib).
    Returns the payload dict {sub, role, iat, exp} on success, None on failure.
    Never raises.

    NOTE: Your JWT payload (from auth.py create_token) only contains:
      sub, role, iat, exp
    There is NO name field in the token — we must look up the name separately.
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
    """
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None

<<<<<<< HEAD
        import jwt as pyjwt  # PyJWT

        secret = os.environ.get("JWT_SECRET", "")
        if not secret:
            return None

        return pyjwt.decode(token, secret, algorithms=["HS256"])
=======
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

>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
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
<<<<<<< HEAD
    Return identity info from the JWT if present and valid,
    otherwise return Unknown / guest defaults.
=======
    Decode the JWT → get uid → look up name from DB.
    Returns full identity dict, or guest defaults if no valid token.
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
    """
    payload = _decode_jwt_soft(request)

    if payload:
<<<<<<< HEAD
        uid = (
            payload.get("sub")
            or payload.get("uid")
            or payload.get("id")
        )
        name = (
            payload.get("full_name")
            or payload.get("display_name")
            or payload.get("username")
            or payload.get("name")
            or "Unknown"
        )
=======
        uid  = payload.get("sub") or payload.get("uid") or ""
        role = payload.get("role", "user")

        # Look up real name from users collection
        # (JWT only has sub/role/iat/exp — no name field)
        name = _lookup_user_name(uid) if uid else "Unknown"

>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
        return {
            "name":         name,
            "uid":          uid,
            "role":         payload.get("role", "user"),
            "is_logged_in": True,
        }
    return {
        "name":         "Unknown",
        "uid":          None,
        "role":         None,
        "is_logged_in": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
<<<<<<< HEAD
# PATHS TO SKIP — health checks, static files, docs (noise)
=======
# PATHS TO SKIP — health, static, docs (noise)
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
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
<<<<<<< HEAD

    Runs AFTER the route handler so DDoS-blocked requests are never counted.
    The DB write is fire-and-forget (asyncio.create_task) so it never adds
    latency to the response.
=======
    Fire-and-forget — never blocks the response.
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return response

        ip   = _get_client_ip(request)
        user = _extract_user_info(request)

<<<<<<< HEAD
        # Fire-and-forget — does not block the response
=======
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
        asyncio.create_task(_upsert_visitor(ip, user))

        return response


async def _upsert_visitor(ip: str, user: dict) -> None:
    """
<<<<<<< HEAD
    Upsert the visitor record in MongoDB.
    One document per unique IP. On repeated visits:
      - last_seen and visit_count are always updated.
      - If the user is now logged in, name/uid/role are upgraded.
      - A known name is never overwritten by "Unknown".
    """
    try:
        from mongodb_client import db  # late import avoids circular dependency
=======
    Upsert visitor record in the 'visitors' collection.
    Uses db.collection() — matches the rest of STEAMI's DB access pattern.

    Rules:
      - last_seen and visit_count always updated.
      - Logged-in user → always upgrade name/uid/role.
      - Guest → set "Unknown" only on first insert, never overwrite a real name.
    """
    try:
        from mongodb_client import db
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832

        now          = _now()
        existing_doc = db.collection("visitors").document(ip).get()

<<<<<<< HEAD
        # Fields written ONLY on first insert
        set_on_insert: dict = {
            "ip":         ip,
            "first_seen": now,
        }

        # Fields always updated on every visit
        set_always: dict = {"last_seen": now}

        if user["is_logged_in"]:
            # Upgrade identity whenever we have a real user
            set_always["name"]         = user["name"]
            set_always["uid"]          = user["uid"]
            set_always["role"]         = user["role"]
            set_always["is_logged_in"] = True
        else:
            # Only set Unknown on first insert — never overwrite a real name
            set_on_insert["name"]         = "Unknown"
            set_on_insert["uid"]          = None
            set_on_insert["role"]         = None
            set_on_insert["is_logged_in"] = False

        db.db["visitors"].update_one(
            {"_id": ip},
            {
                "$set":         set_always,
                "$setOnInsert": set_on_insert,
                "$inc":         {"visit_count": 1},
            },
            upsert=True,
        )
=======
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
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832

    except Exception as e:
        log.debug("visitor_tracker: upsert failed for %s: %s", ip, e)


# ─────────────────────────────────────────────────────────────────────────────
<<<<<<< HEAD
# ROUTER — built lazily so auth imports happen after app init
# ─────────────────────────────────────────────────────────────────────────────

def _build_router() -> APIRouter:
    """
    Build and return the admin router with live auth dependencies.
    Called once from add_visitor_tracking() during app startup.
    Deferred to avoid circular imports between visitor_tracker ↔ auth.
    """
=======
# ROUTER
# ─────────────────────────────────────────────────────────────────────────────

def _build_router() -> APIRouter:
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
    from auth import require_admin

    r = APIRouter()

    # ── GET /api/visitors ────────────────────────────────────────────────────
    @r.get("", summary="List unique IP visitors — ADMIN ONLY")
    def list_visitors(
        limit:     int            = Query(100, ge=1, le=1000, description="Max records"),
        skip:      int            = Query(0,   ge=0,          description="Pagination offset"),
<<<<<<< HEAD
        logged_in: Optional[bool] = Query(None,               description="true=logged-in, false=guest, omit=all"),
        _auth:     dict           = Depends(require_admin),
    ):
        """
        Returns all unique IP visitor records sorted by last_seen (newest first).
        Supports pagination and an optional logged_in boolean filter.
        ADMIN ONLY.
        """
        from mongodb_client import db

        try:
            filt: dict = {}
            if logged_in is True:
                filt["is_logged_in"] = True
            elif logged_in is False:
                filt["is_logged_in"] = False

            cursor = (
                db.db["visitors"]
                .find(filt, {"_id": 0})
                .sort("last_seen", -1)
                .skip(skip)
                .limit(limit)
            )
            visitors = list(cursor)
            total    = db.db["visitors"].count_documents(filt)
=======
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
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832

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
<<<<<<< HEAD
        """
        Returns aggregate visitor statistics:
          total unique IPs, logged-in count, unknown/guest count,
          most recent visit timestamp, top 5 most frequent IPs.
        ADMIN ONLY.
        """
        from mongodb_client import db

=======
        from mongodb_client import db
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
        try:
            all_docs = db.collection("visitors").stream_all()
            visitors = [d.to_dict() for d in all_docs]

<<<<<<< HEAD
            total     = col.count_documents({})
            logged_in = col.count_documents({"is_logged_in": True})
            unknown   = col.count_documents({"is_logged_in": False})

            latest_doc   = col.find_one({}, sort=[("last_seen", -1)])
            latest_visit = latest_doc["last_seen"] if latest_doc else None

            top_visitors = list(
                col.find(
                    {},
                    {"_id": 0, "ip": 1, "name": 1, "visit_count": 1, "last_seen": 1},
                ).sort("visit_count", -1).limit(5)
=======
            total     = len(visitors)
            logged_in = sum(1 for v in visitors if v.get("is_logged_in"))
            unknown   = total - logged_in

            sorted_by_time = sorted(
                visitors, key=lambda v: v.get("last_seen", ""), reverse=True
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
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
<<<<<<< HEAD
        """
        Remove a single visitor record by IP address.
        ADMIN ONLY.
        """
        from mongodb_client import db

=======
        from mongodb_client import db
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
        try:
            doc = db.collection("visitors").document(ip_addr).get()
            if not doc.exists:
                raise HTTPException(404, detail=f"Visitor {ip_addr} not found")
<<<<<<< HEAD
=======
            db.collection("visitors").document(ip_addr).delete()
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
            log.info("visitor_tracker: deleted record for %s", ip_addr)
            return {"deleted": True, "ip": ip_addr}
        except HTTPException:
            raise
        except Exception as e:
            log.error("delete_visitor: %s", e)
            raise HTTPException(500, detail=str(e))

    return r


# ─────────────────────────────────────────────────────────────────────────────
<<<<<<< HEAD
# PUBLIC ENTRY POINT — call this from main.py
=======
# PUBLIC ENTRY POINT
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832
# ─────────────────────────────────────────────────────────────────────────────

def add_visitor_tracking(app) -> APIRouter:
    """
<<<<<<< HEAD
    Attach the VisitorTrackingMiddleware to the FastAPI app and return the
    admin router ready to be registered.

    Call in main.py AFTER add_ddos_protection(app):
=======
    Attach VisitorTrackingMiddleware and return the admin router.
>>>>>>> c22c3346a0b5d79561959ae9360dea29cc7dd832

    In main.py (AFTER add_ddos_protection):
        from visitor_tracker import add_visitor_tracking
        visitors_router = add_visitor_tracking(app)
        app.include_router(visitors_router, prefix="/api/visitors", tags=["Visitors"])
    """
    app.add_middleware(VisitorTrackingMiddleware)
    live_router = _build_router()
    log.info("Visitor tracking active — unique IPs recorded in 'visitors' collection")
    return live_router
