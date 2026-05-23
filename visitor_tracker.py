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
  - Admin-only endpoints:
      GET    /api/visitors        — list all unique visitors (paginated)
      GET    /api/visitors/stats  — aggregated stats (total, logged-in, unknown)
      DELETE /api/visitors/{ip}   — remove a visitor record

COLLECTION SCHEMA (visitors):
  {
    "_id":         "<IP address>",   # unique key = IP
    "ip":          "1.2.3.4",
    "name":        "Sahil Tiwari",   # or "Unknown"
    "uid":         "abc123",         # user ID, or null
    "role":        "user",           # or null
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
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
    Mirrors ddos_protection.py so both systems agree on the IP.
    Checks X-Forwarded-For (nginx / Cloudflare) first, then direct host.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"

    # Strip port from IPv4 e.g. "1.2.3.4:5000" → "1.2.3.4"
    ip = ip.split(":")[0] if "." in ip else ip
    return ip or "unknown"


def _decode_jwt_soft(request: Request) -> Optional[dict]:
    """
    Silently try to decode the Bearer JWT from the Authorization header.
    Returns payload dict on success, None on any failure.
    Never raises — used for optional enrichment only.
    """
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return None

        import jwt as pyjwt  # PyJWT

        secret = os.environ.get("JWT_SECRET", "")
        if not secret:
            return None

        return pyjwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return None


def _extract_user_info(request: Request) -> dict:
    """
    Return identity info from the JWT if present and valid,
    otherwise return Unknown / guest defaults.
    """
    payload = _decode_jwt_soft(request)
    if payload:
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
# PATHS TO SKIP — health checks, static files, docs (noise)
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

    Runs AFTER the route handler so DDoS-blocked requests are never counted.
    The DB write is fire-and-forget (asyncio.create_task) so it never adds
    latency to the response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return response

        ip   = _get_client_ip(request)
        user = _extract_user_info(request)

        # Fire-and-forget — does not block the response
        asyncio.create_task(_upsert_visitor(ip, user))

        return response


async def _upsert_visitor(ip: str, user: dict) -> None:
    """
    Upsert the visitor record in MongoDB.
    One document per unique IP. On repeated visits:
      - last_seen and visit_count are always updated.
      - If the user is now logged in, name/uid/role are upgraded.
      - A known name is never overwritten by "Unknown".
    """
    try:
        from mongodb_client import db  # late import avoids circular dependency

        now = _now()

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

    except Exception as e:
        log.debug("visitor_tracker: upsert failed for %s: %s", ip, e)


# ─────────────────────────────────────────────────────────────────────────────
# ROUTER — built lazily so auth imports happen after app init
# ─────────────────────────────────────────────────────────────────────────────

def _build_router() -> APIRouter:
    """
    Build and return the admin router with live auth dependencies.
    Called once from add_visitor_tracking() during app startup.
    Deferred to avoid circular imports between visitor_tracker ↔ auth.
    """
    from auth import require_admin

    r = APIRouter()

    # ── GET /api/visitors ────────────────────────────────────────────────────
    @r.get("", summary="List unique IP visitors — ADMIN ONLY")
    def list_visitors(
        limit:     int            = Query(100, ge=1, le=1000, description="Max records"),
        skip:      int            = Query(0,   ge=0,          description="Pagination offset"),
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

            return {
                "total":    total,
                "returned": len(visitors),
                "skip":     skip,
                "limit":    limit,
                "visitors": visitors,
            }
        except Exception as e:
            log.error("list_visitors: %s", e)
            raise HTTPException(500, detail=str(e))

    # ── GET /api/visitors/stats ──────────────────────────────────────────────
    @r.get("/stats", summary="Visitor stats summary — ADMIN ONLY")
    def visitor_stats(_auth: dict = Depends(require_admin)):
        """
        Returns aggregate visitor statistics:
          total unique IPs, logged-in count, unknown/guest count,
          most recent visit timestamp, top 5 most frequent IPs.
        ADMIN ONLY.
        """
        from mongodb_client import db

        try:
            col = db.db["visitors"]

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
            )

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
        """
        Remove a single visitor record by IP address.
        ADMIN ONLY.
        """
        from mongodb_client import db

        try:
            result = db.db["visitors"].delete_one({"ip": ip_addr})
            if result.deleted_count == 0:
                raise HTTPException(404, detail=f"Visitor {ip_addr} not found")
            log.info("visitor_tracker: deleted record for %s", ip_addr)
            return {"deleted": True, "ip": ip_addr}
        except HTTPException:
            raise
        except Exception as e:
            log.error("delete_visitor: %s", e)
            raise HTTPException(500, detail=str(e))

    return r


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT — call this from main.py
# ─────────────────────────────────────────────────────────────────────────────

def add_visitor_tracking(app) -> APIRouter:
    """
    Attach the VisitorTrackingMiddleware to the FastAPI app and return the
    admin router ready to be registered.

    Call in main.py AFTER add_ddos_protection(app):

        from visitor_tracker import add_visitor_tracking
        visitors_router = add_visitor_tracking(app)
        app.include_router(visitors_router, prefix="/api/visitors", tags=["Visitors"])
    """
    app.add_middleware(VisitorTrackingMiddleware)
    live_router = _build_router()
    log.info("Visitor tracking active — unique IPs recorded in 'visitors' collection")
    return live_router
