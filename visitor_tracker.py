"""
visitor_tracker.py  —  Unique IP Visitor Tracking for STEAMI
=============================================================
Tracks every unique IP address that hits the backend.
If the request carries a valid JWT, stores the user's name.
If not logged in (or token missing/invalid), stores "Unknown".

HOW IT WORKS:
  - A Starlette middleware intercepts every request AFTER DDoS protection.
  - It extracts the real client IP (same logic as ddos_protection.py).
  - It optionally decodes the JWT to get user name & role.
  - It upserts a document in the "visitors" MongoDB collection keyed on IP.
  - Only ONE document per unique IP — updates name/last_seen on repeat visits.
  - Two admin-only endpoints:
      GET  /api/visitors        — list all unique visitors (paginated)
      GET  /api/visitors/stats  — aggregated stats (total, logged-in, unknown)
      DELETE /api/visitors/{ip} — remove a visitor record

COLLECTION SCHEMA (visitors):
  {
    "_id":        "<IP address>",          # unique key
    "ip":         "1.2.3.4",
    "name":       "Sahil Tiwari",          # or "Unknown"
    "uid":        "abc123",               # user ID, or null
    "role":       "user",                  # or null
    "first_seen": "2025-04-01T10:00:00Z",
    "last_seen":  "2025-04-15T18:32:00Z",
    "visit_count": 42,
    "is_logged_in": true
  }

USAGE — add to main.py:
  from visitor_tracker import add_visitor_tracking, router as visitors_router
  add_visitor_tracking(app)               # ← AFTER add_ddos_protection(app)
  app.include_router(visitors_router, prefix="/api/visitors", tags=["Visitors"])
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _get_client_ip(request: Request) -> str:
    """
    Extract the real client IP.
    Mirrors the logic in ddos_protection.py so both systems agree on the IP.
    Checks X-Forwarded-For (set by nginx / Cloudflare) first, then direct host.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"

    # Strip port number from IPv4 (e.g. "1.2.3.4:5000" → "1.2.3.4")
    ip = ip.split(":")[0] if "." in ip else ip
    return ip or "unknown"


def _decode_jwt_soft(request: Request) -> Optional[dict]:
    """
    Try to decode the Bearer JWT from the Authorization header.
    Returns the payload dict on success, None if missing or invalid.
    Never raises — used for optional auth enrichment only.
    """
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None

        # Import here to avoid circular imports at module level
        import os
        import jwt as pyjwt  # PyJWT

        secret = os.environ.get("JWT_SECRET", "")
        if not secret:
            return None

        payload = pyjwt.decode(token, secret, algorithms=["HS256"])
        return payload
    except Exception:
        return None


def _extract_user_info(request: Request) -> dict:
    """
    Extract user identity from the JWT (if present and valid).
    Returns a dict with name, uid, role, is_logged_in.
    Falls back to Anonymous / Unknown if no valid token.
    """
    payload = _decode_jwt_soft(request)
    if payload:
        # JWT payload fields used across STEAMI's auth system:
        # sub = uid, full_name / display_name / username = name, role = role
        uid  = payload.get("sub") or payload.get("uid") or payload.get("id")
        name = (
            payload.get("full_name")
            or payload.get("display_name")
            or payload.get("username")
            or payload.get("name")
            or "Unknown"
        )
        role = payload.get("role", "user")
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
# PATHS TO SKIP (health checks, static files, etc.)
# ─────────────────────────────────────────────────────────────────────────────

# Don't track requests to these prefixes — they're noise
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
    Starlette middleware that records unique visitor IPs after every request.

    Runs AFTER the route handler so:
      a) DDoS-blocked requests are never counted (they never reach a route).
      b) The JWT is already validated upstream if the route required auth —
         we're just reading the same token passively for enrichment.

    Uses a fire-and-forget background approach: visitor updates are enqueued
    and written asynchronously so they never slow down the response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Process the actual request first
        response = await call_next(request)

        # Skip noisy / irrelevant paths
        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return response

        # Record visitor in the background (non-blocking)
        ip   = _get_client_ip(request)
        user = _extract_user_info(request)

        # Use asyncio.create_task to avoid blocking the response
        import asyncio
        asyncio.create_task(_upsert_visitor(ip, user))

        return response


async def _upsert_visitor(ip: str, user: dict) -> None:
    """
    Upsert the visitor record in MongoDB.
    Uses update_one with upsert=True so only ONE document exists per IP.
    On first visit: creates the document with first_seen.
    On subsequent visits: updates last_seen, visit_count, and optionally name
      (upgrades "Unknown" → real name if user logs in later).
    """
    try:
        from mongodb_client import db  # imported here to avoid circular import at startup

        now = _now()

        # If a logged-in user is visiting, always update name/uid/role.
        # If unknown, only set name on INSERT (don't overwrite a known name).
        set_on_insert = {
            "ip":         ip,
            "first_seen": now,
        }

        # Fields that always get updated
        set_always: dict = {
            "last_seen": now,
        }

        # Only update identity fields if we have a logged-in user
        if user["is_logged_in"]:
            set_always["name"]         = user["name"]
            set_always["uid"]          = user["uid"]
            set_always["role"]         = user["role"]
            set_always["is_logged_in"] = True
        else:
            # For upsert: set name="Unknown" only on INSERT (don't overwrite real name)
            set_on_insert["name"]         = "Unknown"
            set_on_insert["uid"]          = None
            set_on_insert["role"]         = None
            set_on_insert["is_logged_in"] = False

        # MongoDB update pipeline:
        # $set: always-updated fields
        # $setOnInsert: only written on first insert
        # $inc: increment visit_count by 1
        db.db["visitors"].update_one(
            {"_id": ip},
            {
                "$set":          set_always,
                "$setOnInsert":  set_on_insert,
                "$inc":          {"visit_count": 1},
            },
            upsert=True,
        )

    except Exception as e:
        # Non-fatal — visitor tracking should never break the app
        log.debug("visitor_tracker: upsert failed for %s: %s", ip, e)


# ─────────────────────────────────────────────────────────────────────────────
# API ROUTER — admin-only endpoints
# ─────────────────────────────────────────────────────────────────────────────

router = APIRouter()


@router.get(
    "",
    summary="List all unique IP visitors — ADMIN ONLY",
)
def list_visitors(
    limit:      int  = Query(100, ge=1,  le=1000, description="Max records to return"),
    skip:       int  = Query(0,   ge=0,            description="Pagination offset"),
    logged_in:  bool | None = Query(None,           description="Filter: true=logged-in only, false=unknown only"),
    payload:    dict = Depends(None),  # replaced below
):
    """
    GET /api/visitors?limit=100&skip=0
    Returns all unique IP visitor records, newest first.
    Optional filter: ?logged_in=true to see only authenticated visitors.
    ADMIN ONLY.
    """
    pass  # replaced by the actual implementation with auth below


@router.get(
    "/stats",
    summary="Visitor stats summary — ADMIN ONLY",
)
def visitor_stats(payload: dict = Depends(None)):
    """
    GET /api/visitors/stats
    Returns aggregate counts: total unique IPs, logged-in vs unknown.
    ADMIN ONLY.
    """
    pass  # replaced below


@router.delete(
    "/{ip}",
    summary="Remove a visitor record — ADMIN ONLY",
)
def delete_visitor(ip: str, payload: dict = Depends(None)):
    """
    DELETE /api/visitors/{ip}
    Remove a single visitor record by IP address.
    ADMIN ONLY.
    """
    pass  # replaced below


# Re-define the routes with proper auth dependencies after the stubs above.
# This pattern avoids a circular import from auth.py at module load time.

def _build_router() -> APIRouter:
    """
    Build the actual router with live auth dependencies.
    Called from add_visitor_tracking() in main.py.
    """
    from fastapi import APIRouter, Depends, HTTPException, Query
    from auth import require_admin
    from mongodb_client import db

    r = APIRouter()

    @r.get("", summary="List all unique IP visitors — ADMIN ONLY")
    def _list_visitors(
        limit:     int       = Query(100, ge=1,  le=1000),
        skip:      int       = Query(0,   ge=0),
        logged_in: bool | None = Query(None),
        _auth:     dict      = Depends(require_admin),
    ):
        """
        List all unique IP visitor records, sorted by last_seen descending.
        Supports pagination (skip/limit) and a logged_in boolean filter.
        """
        try:
            query: dict = {}
            if logged_in is True:
                query["is_logged_in"] = True
            elif logged_in is False:
                query["is_logged_in"] = False

            cursor = (
                db.db["visitors"]
                .find(query, {"_id": 0})               # exclude Mongo _id from output
                .sort("last_seen", -1)                  # newest first
                .skip(skip)
                .limit(limit)
            )

            visitors = list(cursor)
            total    = db.db["visitors"].count_documents(query)

            return {
                "total":    total,
                "returned": len(visitors),
                "skip":     skip,
                "limit":    limit,
                "visitors": visitors,
            }
        except Exception as e:
            log.error("list_visitors error: %s", e)
            raise HTTPException(500, detail=str(e))

    @r.get("/stats", summary="Visitor stats summary — ADMIN ONLY")
    def _visitor_stats(_auth: dict = Depends(require_admin)):
        """
        Returns aggregate visitor statistics:
          - total unique IPs
          - logged_in count (users with a valid JWT at time of visit)
          - unknown count (no JWT / not logged in)
          - most recent visit timestamp
          - top 5 most frequent visitors
        """
        try:
            col = db.db["visitors"]

            total      = col.count_documents({})
            logged_in  = col.count_documents({"is_logged_in": True})
            unknown    = col.count_documents({"is_logged_in": False})

            # Most recent visit across all IPs
            latest_doc = col.find_one({}, sort=[("last_seen", -1)])
            latest     = latest_doc["last_seen"] if latest_doc else None

            # Top 5 by visit_count
            top_visitors = list(
                col.find({}, {"_id": 0, "ip": 1, "name": 1, "visit_count": 1, "last_seen": 1})
                   .sort("visit_count", -1)
                   .limit(5)
            )

            return {
                "total_unique_ips": total,
                "logged_in":        logged_in,
                "unknown":          unknown,
                "latest_visit":     latest,
                "top_visitors":     top_visitors,
            }
        except Exception as e:
            log.error("visitor_stats error: %s", e)
            raise HTTPException(500, detail=str(e))

    @r.delete("/{ip_addr}", summary="Remove a visitor record — ADMIN ONLY")
    def _delete_visitor(ip_addr: str, _auth: dict = Depends(require_admin)):
        """
        Remove a single visitor record by IP address.
        URL-encode dots: e.g. /api/visitors/1.2.3.4
        """
        try:
            result = db.db["visitors"].delete_one({"ip": ip_addr})
            if result.deleted_count == 0:
                raise HTTPException(404, detail=f"Visitor {ip_addr} not found")
            log.info("visitor deleted: %s", ip_addr)
            return {"deleted": True, "ip": ip_addr}
        except HTTPException:
            raise
        except Exception as e:
            log.error("delete_visitor error: %s", e)
            raise HTTPException(500, detail=str(e))

    return r


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE FUNCTION — call this from main.py
# ─────────────────────────────────────────────────────────────────────────────

def add_visitor_tracking(app) -> APIRouter:
    """
    Register the VisitorTrackingMiddleware on the FastAPI app AND
    return the fully-wired admin router to be included in main.py.

    Usage in main.py (AFTER add_ddos_protection):

        from visitor_tracker import add_visitor_tracking
        visitors_router = add_visitor_tracking(app)
        app.include_router(visitors_router, prefix="/api/visitors", tags=["Visitors"])

    Returns the router so main.py controls the prefix and tags.
    """
    # Add the middleware — runs on every request AFTER route handling
    app.add_middleware(VisitorTrackingMiddleware)

    # Build and return the router with live auth dependencies
    live_router = _build_router()

    log.info("Visitor tracking active — recording unique IPs to 'visitors' collection")
    return live_router
