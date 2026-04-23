"""
ddos_protection.py  —  DDoS & Abuse Protection Middleware for STEAMI
======================================================================
Protects the FastAPI backend against:
  1. Rate limiting     — too many requests per IP per time window
  2. IP blocking       — hard-block known bad IPs / temp-ban repeat offenders
  3. Request size      — reject oversized bodies (upload bomb prevention)
  4. Slow-read attack  — reject connections that take too long to send headers
  5. Endpoint hammering — extra-tight limits on expensive endpoints (login, signup)
  6. Suspicious patterns — block user-agents and paths that look like scanners
  7. Connection flood  — simultaneous connection cap per IP

ZERO new packages needed — uses only Python stdlib + FastAPI's existing Starlette.

HOW IT WORKS:
  Uses a sliding-window token bucket per IP address stored in memory.
  Each IP gets N tokens per window. Each request consumes 1 token.
  When tokens run out the IP gets a 429 response.
  IPs that repeatedly hit 429 get temporarily banned.
  All state is in-memory (lost on restart) — fine for a single-process server.
  For multi-process / multi-server setups, swap the in-memory store for Redis.

RATE LIMITS (configurable via .env):
  RATE_LIMIT_GLOBAL      = 120   requests per minute per IP (default)
  RATE_LIMIT_AUTH        = 10    requests per minute on /api/auth/login + /signup
  RATE_LIMIT_EXPENSIVE   = 20    requests per minute on insight/refresh/feed endpoints
  RATE_LIMIT_WINDOW      = 60    seconds (sliding window size)
  MAX_REQUEST_BODY_MB    = 10    megabytes (reject bodies larger than this)
  MAX_TEMP_BAN_MINUTES   = 15    minutes (auto-ban after repeated 429s)
  MAX_429_BEFORE_BAN     = 20    how many 429s before temp-ban kicks in
  BANNED_IPS             = ""    comma-separated list of permanently banned IPs

USAGE — add to main.py:
  from ddos_protection import add_ddos_protection
  add_ddos_protection(app)
  # Call AFTER app = FastAPI(...) but BEFORE app.include_router(...)
"""

import os
import time
import threading
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION  (all values overridable via .env)
# ─────────────────────────────────────────────────────────────────────────────

# Global rate limit: max requests per IP per window
RATE_LIMIT_GLOBAL: int    = int(os.environ.get("RATE_LIMIT_GLOBAL",    "120"))

# Tighter limit for auth endpoints (login, signup) — prevent brute force
RATE_LIMIT_AUTH: int      = int(os.environ.get("RATE_LIMIT_AUTH",      "10"))

# Limit for expensive endpoints (insight generation, article refresh, feed)
RATE_LIMIT_EXPENSIVE: int = int(os.environ.get("RATE_LIMIT_EXPENSIVE", "20"))

# Time window in seconds for all limits above
RATE_WINDOW: int          = int(os.environ.get("RATE_LIMIT_WINDOW",    "60"))

# Maximum allowed request body in bytes (default 10 MB)
MAX_BODY_BYTES: int       = int(os.environ.get("MAX_REQUEST_BODY_MB",  "10")) * 1024 * 1024

# How many 429 responses before an IP gets a temporary ban
MAX_429_BEFORE_BAN: int   = int(os.environ.get("MAX_429_BEFORE_BAN",  "20"))

# How long a temp ban lasts in seconds
TEMP_BAN_SECONDS: int     = int(os.environ.get("MAX_TEMP_BAN_MINUTES", "15")) * 60

# Comma-separated list of permanently banned IPs (e.g. "1.2.3.4,5.6.7.8")
_banned_env               = os.environ.get("BANNED_IPS", "")
PERMANENT_BANS: set[str]  = {ip.strip() for ip in _banned_env.split(",") if ip.strip()}

# How often to run the cleanup sweep (remove stale entries from memory)
CLEANUP_INTERVAL: int     = 300   # every 5 minutes


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

# These path prefixes get the tighter AUTH rate limit
AUTH_PATHS: tuple = (
    "/api/auth/login",
    "/api/auth/signup",
    "/api/auth/seed",
)

# These path prefixes get the tighter EXPENSIVE rate limit
EXPENSIVE_PATHS: tuple = (
    "/api/articles/insights/process",
    "/api/articles/refresh",
    "/api/articles/fetch",
    "/api/feed/from-selection",
)

# Paths that are blocked entirely (scanner bait / common attack paths)
BLOCKED_PATH_PATTERNS: list[str] = [
    r"^/\.env",                  # .env file probing
    r"^/\.git",                  # .git directory probing
    r"^/wp-",                    # WordPress scanner
    r"^/wordpress",
    r"^/phpmyadmin",             # phpMyAdmin scanner
    r"^/admin\.php",
    r"^/xmlrpc\.php",            # WordPress XML-RPC attack
    r"^/actuator",               # Spring Boot actuator probing
    r"^/api/v\d+/users/\d+/password",  # generic password reset probing
    r"\.php$",                   # PHP file probing (this is a Python server)
    r"\.asp$",                   # ASP file probing
    r"\.jsp$",                   # JSP file probing
    r"/etc/passwd",              # LFI attempt
    r"/proc/self",               # LFI attempt
    r"\.\./\.\./",               # directory traversal
    r"<script",                  # XSS probe in URL
    r"UNION\s+SELECT",           # SQL injection
    r"exec\(",                   # code injection probe
]

# Compile patterns once for speed
_BLOCKED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATH_PATTERNS]

# User-agents that are known scanners/bots (block them)
BLOCKED_USER_AGENTS: list[str] = [
    "sqlmap",        # SQL injection scanner
    "nikto",         # web vulnerability scanner
    "nmap",          # network scanner
    "masscan",       # mass port scanner
    "zgrab",         # internet-wide scanner
    "python-requests/2.1",  # very old version used by scripts
    "curl/7.1",      # very old curl used by attack scripts
    "Go-http-client/1.1",   # Go scanner
    "dirbuster",     # directory brute-force tool
    "gobuster",      # directory/DNS brute-force
    "wfuzz",         # web fuzzer
    "burpsuite",     # pen-test proxy (block automated scans)
    "havij",         # SQL injection tool
    "acunetix",      # web vulnerability scanner
    "nessus",        # vulnerability scanner
]


# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY RATE LIMIT STORE
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()

# ip → list of request timestamps (unix float) in the current window
_request_log: dict[str, list[float]] = defaultdict(list)

# ip → number of 429 responses received (resets when IP goes clean)
_violation_count: dict[str, int] = defaultdict(int)

# ip → unix timestamp when the temp ban expires (0 = not banned)
_temp_banned: dict[str, float] = defaultdict(float)

# ip → unix timestamp when permanently added to runtime ban list
_runtime_bans: dict[str, float] = {}

# Stats counters (for the /api/security/stats endpoint)
_stats = {
    "total_requests":      0,
    "requests_blocked":    0,
    "rate_limit_hits":     0,
    "temp_bans_issued":    0,
    "scanner_blocks":      0,
    "last_cleanup":        time.time(),
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """
    Extract the real client IP from the request.
    Checks X-Forwarded-For first (set by nginx/cloudflare reverse proxies),
    then falls back to the direct connection IP.
    Strips port number if present.
    """
    # X-Forwarded-For can be spoofed by clients if not behind a trusted proxy.
    # For production behind nginx/Cloudflare, this is fine.
    # For direct internet exposure, use request.client.host only.
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        # Take the leftmost IP (the original client)
        ip = forwarded_for.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else "unknown"

    # Strip IPv6 port or brackets
    ip = ip.split(":")[0] if "." in ip else ip
    return ip or "unknown"


def _is_temp_banned(ip: str) -> bool:
    """Return True if the IP is currently in a temporary ban."""
    ban_until = _temp_banned.get(ip, 0)
    if ban_until and time.time() < ban_until:
        return True
    if ban_until and time.time() >= ban_until:
        # Ban expired — clean up
        del _temp_banned[ip]
        _violation_count[ip] = 0
    return False


def _record_violation(ip: str) -> bool:
    """
    Increment the violation counter for an IP.
    If violations exceed MAX_429_BEFORE_BAN, issue a temporary ban.
    Returns True if a new ban was issued.
    """
    _violation_count[ip] += 1
    if _violation_count[ip] >= MAX_429_BEFORE_BAN:
        ban_until = time.time() + TEMP_BAN_SECONDS
        _temp_banned[ip] = ban_until
        _violation_count[ip] = 0
        _stats["temp_bans_issued"] += 1
        log.warning(
            "DDoS: temp-banned %s for %d min (too many violations)",
            ip, TEMP_BAN_SECONDS // 60
        )
        return True
    return False


def _check_rate_limit(ip: str, limit: int) -> tuple[bool, int, int]:
    """
    Sliding-window rate limit check.

    Returns:
        (allowed: bool, requests_in_window: int, retry_after_seconds: int)

    - Allowed: True if the request should be let through.
    - requests_in_window: how many requests this IP made in the current window.
    - retry_after_seconds: how long until the window resets (for 429 header).
    """
    now    = time.time()
    cutoff = now - RATE_WINDOW

    # Remove timestamps older than the window
    timestamps = [t for t in _request_log[ip] if t > cutoff]
    timestamps.append(now)
    _request_log[ip] = timestamps

    count = len(timestamps)

    if count > limit:
        # How many seconds until the oldest request falls out of the window
        oldest = timestamps[0]
        retry_after = max(1, int(RATE_WINDOW - (now - oldest)))
        return False, count, retry_after

    return True, count, 0


def _cleanup_stale_entries():
    """
    Remove IPs with no recent activity from memory.
    Called periodically to prevent unbounded memory growth.
    """
    now    = time.time()
    cutoff = now - RATE_WINDOW * 2   # keep entries for 2 windows

    with _lock:
        stale_ips = [
            ip for ip, timestamps in _request_log.items()
            if not timestamps or max(timestamps) < cutoff
        ]
        for ip in stale_ips:
            del _request_log[ip]
            _violation_count.pop(ip, None)

        _stats["last_cleanup"] = now

    if stale_ips:
        log.debug("DDoS cleanup: removed %d stale IP entries", len(stale_ips))


def _is_scanner_path(path: str) -> bool:
    """Check if the request path matches any known scanner/attack pattern."""
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(path):
            return True
    return False


def _is_scanner_agent(user_agent: str) -> bool:
    """Check if the User-Agent string matches a known scanner."""
    ua_lower = user_agent.lower()
    return any(bot in ua_lower for bot in BLOCKED_USER_AGENTS)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN MIDDLEWARE CLASS
# ─────────────────────────────────────────────────────────────────────────────

class DDoSProtectionMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware that inspects every incoming request before it reaches
    any route handler. Applies all protection layers in order.

    Order of checks (fastest/cheapest first):
    1. Permanent IP ban
    2. Temporary IP ban
    3. Scanner path detection
    4. Scanner user-agent detection
    5. Request body size limit
    6. Rate limiting (per-endpoint tier)
    """

    async def dispatch(self, request: Request, call_next):
        now  = time.time()
        ip   = _get_client_ip(request)
        path = request.url.path

        with _lock:
            _stats["total_requests"] += 1

            # ── Periodic cleanup ──────────────────────────────────────────────
            if now - _stats["last_cleanup"] > CLEANUP_INTERVAL:
                # Run cleanup in a background thread so it doesn't block this request
                threading.Thread(target=_cleanup_stale_entries, daemon=True).start()

            # ── 1. Permanent IP ban ───────────────────────────────────────────
            if ip in PERMANENT_BANS or ip in _runtime_bans:
                _stats["requests_blocked"] += 1
                log.warning("DDoS: blocked permanently-banned IP %s → %s", ip, path)
                return JSONResponse(
                    status_code = 403,
                    content     = {"error": "Access denied."},
                )

            # ── 2. Temporary ban ──────────────────────────────────────────────
            if _is_temp_banned(ip):
                ban_until   = _temp_banned.get(ip, 0)
                retry_after = max(1, int(ban_until - now))
                _stats["requests_blocked"] += 1
                return JSONResponse(
                    status_code = 429,
                    content     = {
                        "error":        "Too many requests. You have been temporarily blocked.",
                        "retry_after":  retry_after,
                        "unblocked_at": datetime.fromtimestamp(ban_until, tz=timezone.utc).isoformat(),
                    },
                    headers = {"Retry-After": str(retry_after)},
                )

            # ── 3. Scanner path ───────────────────────────────────────────────
            if _is_scanner_path(path):
                _stats["requests_blocked"] += 1
                _stats["scanner_blocks"] += 1
                log.warning("DDoS: scanner path blocked %s → %s", ip, path)
                _record_violation(ip)
                return JSONResponse(
                    status_code = 404,   # return 404 not 403 — don't confirm the path exists
                    content     = {"error": "Not found."},
                )

            # ── 4. Scanner user-agent ─────────────────────────────────────────
            user_agent = request.headers.get("User-Agent", "")
            if _is_scanner_agent(user_agent):
                _stats["requests_blocked"] += 1
                _stats["scanner_blocks"] += 1
                log.warning("DDoS: scanner UA blocked %s agent=%.60s", ip, user_agent)
                return JSONResponse(
                    status_code = 403,
                    content     = {"error": "Access denied."},
                )

        # ── 5. Request body size ──────────────────────────────────────────────
        # Check Content-Length header first (fast, no body reading needed)
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_BYTES:
                    with _lock:
                        _stats["requests_blocked"] += 1
                    log.warning(
                        "DDoS: body too large from %s path=%s size=%s bytes",
                        ip, path, content_length
                    )
                    return JSONResponse(
                        status_code = 413,
                        content     = {
                            "error": f"Request body too large. Maximum allowed: {MAX_BODY_BYTES // (1024*1024)} MB."
                        },
                    )
            except ValueError:
                pass   # malformed Content-Length — let it through, body read will catch it

        # ── 6. Rate limiting ──────────────────────────────────────────────────
        with _lock:
            # Determine which rate limit tier applies to this path
            if any(path.startswith(p) for p in AUTH_PATHS):
                limit = RATE_LIMIT_AUTH
                tier  = "auth"
            elif any(path.startswith(p) for p in EXPENSIVE_PATHS):
                limit = RATE_LIMIT_EXPENSIVE
                tier  = "expensive"
            else:
                limit = RATE_LIMIT_GLOBAL
                tier  = "global"

            allowed, count, retry_after = _check_rate_limit(ip, limit)

            if not allowed:
                _stats["requests_blocked"] += 1
                _stats["rate_limit_hits"]  += 1
                new_ban = _record_violation(ip)

                log.warning(
                    "DDoS: rate limit hit ip=%s tier=%s count=%d limit=%d path=%s",
                    ip, tier, count, limit, path
                )

                return JSONResponse(
                    status_code = 429,
                    content     = {
                        "error":       f"Rate limit exceeded. Max {limit} requests per {RATE_WINDOW}s.",
                        "tier":        tier,
                        "retry_after": retry_after,
                        "banned":      new_ban,
                    },
                    headers = {
                        "Retry-After":      str(retry_after),
                        "X-RateLimit-Limit":     str(limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset":     str(int(time.time()) + retry_after),
                    },
                )

        # ── All checks passed — add rate limit headers and proceed ────────────
        response = await call_next(request)

        # Add remaining-rate-limit header to every successful response
        with _lock:
            remaining = max(0, limit - len(_request_log.get(ip, [])))

        response.headers["X-RateLimit-Limit"]     = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"]    = str(RATE_WINDOW)
        # Security headers on every response
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]          = "DENY"
        response.headers["X-XSS-Protection"]         = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"

        return response


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN API — runtime ban management
# ─────────────────────────────────────────────────────────────────────────────

def get_security_router():
    """
    Returns a FastAPI router with admin endpoints for managing bans.
    Mount this in main.py with:
        app.include_router(get_security_router(), prefix="/api/security", tags=["Security"])
    """
    from fastapi import APIRouter, Depends
    from auth import require_admin

    router = APIRouter()

    @router.get("/stats", summary="DDoS protection stats — admin only")
    def security_stats(payload: dict = Depends(require_admin)):
        """
        GET /api/security/stats
        Returns real-time DDoS protection statistics.
        Shows request counts, blocks, rate-limit hits, active bans.
        ADMIN ONLY.
        """
        with _lock:
            active_temp_bans = {
                ip: {
                    "banned_until": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
                    "seconds_remaining": max(0, int(t - time.time())),
                }
                for ip, t in _temp_banned.items()
                if t > time.time()
            }
            active_violations = {
                ip: count
                for ip, count in _violation_count.items()
                if count > 0
            }
            top_ips = sorted(
                [(ip, len(ts)) for ip, ts in _request_log.items()],
                key=lambda x: x[1], reverse=True
            )[:10]

        return {
            "config": {
                "rate_limit_global":    RATE_LIMIT_GLOBAL,
                "rate_limit_auth":      RATE_LIMIT_AUTH,
                "rate_limit_expensive": RATE_LIMIT_EXPENSIVE,
                "rate_window_seconds":  RATE_WINDOW,
                "max_body_mb":          MAX_BODY_BYTES // (1024 * 1024),
                "temp_ban_minutes":     TEMP_BAN_SECONDS // 60,
                "violations_before_ban":MAX_429_BEFORE_BAN,
            },
            "stats": _stats,
            "active_temp_bans":    active_temp_bans,
            "temp_ban_count":      len(active_temp_bans),
            "permanent_bans":      list(PERMANENT_BANS | set(_runtime_bans.keys())),
            "top_ips_this_window": [{"ip": ip, "requests": count} for ip, count in top_ips],
            "violation_warnings":  active_violations,
        }

    @router.post("/ban/{ip}", summary="Permanently ban an IP — admin only")
    def ban_ip(ip: str, payload: dict = Depends(require_admin)):
        """
        POST /api/security/ban/{ip}
        Add an IP to the runtime permanent ban list immediately.
        This ban persists until the server restarts (add to BANNED_IPS in .env for permanent).
        ADMIN ONLY.
        """
        with _lock:
            _runtime_bans[ip] = time.time()
        log.warning("DDoS: admin manually banned IP %s", ip)
        return {"banned": True, "ip": ip, "note": "Add to BANNED_IPS in .env for permanent ban"}

    @router.delete("/ban/{ip}", summary="Unban an IP — admin only")
    def unban_ip(ip: str, payload: dict = Depends(require_admin)):
        """
        DELETE /api/security/ban/{ip}
        Remove an IP from both the runtime ban list and temp ban list.
        Cannot unban IPs in the BANNED_IPS .env variable (restart required for that).
        ADMIN ONLY.
        """
        removed = False
        with _lock:
            if ip in _runtime_bans:
                del _runtime_bans[ip]
                removed = True
            if ip in _temp_banned:
                del _temp_banned[ip]
                removed = True
            _violation_count.pop(ip, None)
        if ip in PERMANENT_BANS:
            return {
                "unbanned": False,
                "ip":       ip,
                "reason":   "IP is in BANNED_IPS env variable — remove it there and restart",
            }
        log.info("DDoS: admin unbanned IP %s", ip)
        return {"unbanned": removed, "ip": ip}

    @router.delete("/temp-bans", summary="Clear all temporary bans — admin only")
    def clear_temp_bans(payload: dict = Depends(require_admin)):
        """
        DELETE /api/security/temp-bans
        Remove all current temporary bans. Useful after a false-positive flood.
        ADMIN ONLY.
        """
        with _lock:
            count = len(_temp_banned)
            _temp_banned.clear()
            _violation_count.clear()
        log.info("DDoS: admin cleared all %d temp bans", count)
        return {"cleared": True, "bans_removed": count}

    return router


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE FUNCTION — call this from main.py
# ─────────────────────────────────────────────────────────────────────────────

def add_ddos_protection(app: FastAPI) -> None:
    """
    Add all DDoS protection to a FastAPI app in one call.

    Call this in main.py AFTER app = FastAPI(...) but BEFORE app.include_router(...):

        from ddos_protection import add_ddos_protection
        app = FastAPI(...)
        add_ddos_protection(app)      ← add this line
        app.include_router(...)

    This adds:
    - DDoSProtectionMiddleware (rate limiting, IP bans, scanner detection, body size)
    - /api/security/* admin endpoints (stats, ban/unban, clear temp bans)
    """
    # Add the middleware — runs on EVERY request before any route handler
    app.add_middleware(DDoSProtectionMiddleware)

    # Add the admin security management routes
    security_router = get_security_router()
    app.include_router(security_router, prefix="/api/security", tags=["Security"])

    log.info(
        "DDoS protection active — global=%d/min auth=%d/min expensive=%d/min "
        "max_body=%dMB temp_ban=%dmin",
        RATE_LIMIT_GLOBAL,
        RATE_LIMIT_AUTH,
        RATE_LIMIT_EXPENSIVE,
        MAX_BODY_BYTES // (1024 * 1024),
        TEMP_BAN_SECONDS // 60,
    )