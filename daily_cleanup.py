"""
daily_cleanup.py — Daily auto-cleanup for STEAMI
==================================================
Deletes articles and their associated ai_insights that are older than
ARTICLE_EXPIRY_DAYS (15) days, and feed_articles (+ their ai_insights)
that are older than FEED_EXPIRY_DAYS (15) days.

HOW IT WORKS:
  - A background daemon thread wakes up every 24 hours.
  - On each run it scans `articles`, `feed_articles`, and `ai_insights`
    collections and deletes documents whose `fetched_at` (or `created_at`
    for ai_insights) timestamp is older than 15 days.
  - The thread is started automatically when you call `start_cleanup_scheduler()`
    from your startup hook.
  - A manual trigger is also available via the admin endpoint:
      POST /api/admin/cleanup

INTEGRATION (add to main.py):
  ① Import at the top:
        from daily_cleanup import start_cleanup_scheduler, run_cleanup_now

  ② Start the scheduler in your startup event:
        @app.on_event("startup")
        def on_startup():
            ...existing code...
            start_cleanup_scheduler()

  ③ (Optional) Register the manual-trigger endpoint:
        from daily_cleanup import cleanup_router
        app.include_router(cleanup_router, prefix="/api/admin", tags=["Admin"])
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from mongodb_client import db
from auth import require_admin, get_uid

log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
ARTICLE_EXPIRY_DAYS = 15             # articles + ai_insights
FEED_EXPIRY_DAYS    = 15             # feed_articles + their ai_insights
CLEANUP_INTERVAL_S  = 24 * 60 * 60  # run once every 24 hours

# ── Thread guard ───────────────────────────────────────────────────────────────
_cleanup_lock    = threading.Lock()
_cleanup_running = False             # True only while a cleanup pass is executing


# ══════════════════════════════════════════════════════════════════════════════
# CORE CLEANUP LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def _cutoff_iso(days: int) -> str:
    """ISO-8601 timestamp for exactly `days` ago (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _delete_collection_docs(collection: str, field: str, cutoff: str) -> dict:
    """
    Delete all documents in `collection` where `field` < `cutoff`.
    Returns a summary dict: {deleted, errors, ids}.
    """
    deleted, errors, ids = 0, 0, []
    try:
        docs = (
            db.collection(collection)
              .where(field, "<", cutoff)
              .stream()
        )
        for doc in docs:
            doc_id = doc.id
            try:
                db.collection(collection).document(doc_id).delete()
                ids.append(doc_id)
                deleted += 1
            except Exception as e:
                log.error("cleanup: failed to delete %s/%s: %s", collection, doc_id, e)
                errors += 1
    except Exception as e:
        log.error("cleanup: query failed on %s: %s", collection, e)
        errors += 1

    return {"deleted": deleted, "errors": errors, "ids": ids}


def run_cleanup_now() -> dict:
    """
    Execute one full cleanup pass synchronously.
    Deletes from:
      • articles        — where fetched_at  < cutoff
      • feed_articles   — where fetched_at  < cutoff
      • ai_insights     — where created_at  < cutoff
      • insight_queue   — where queued_at   < cutoff  (done/failed only)

    Returns a summary dict with per-collection stats.
    """
    global _cleanup_running

    with _cleanup_lock:
        if _cleanup_running:
            log.info("cleanup: already running — skipping this trigger")
            return {"skipped": True, "reason": "cleanup already in progress"}
        _cleanup_running = True

    started_at      = datetime.now(timezone.utc).isoformat()
    article_cutoff  = _cutoff_iso(ARTICLE_EXPIRY_DAYS)
    feed_cutoff     = _cutoff_iso(FEED_EXPIRY_DAYS)
    log.info(
        "cleanup: starting — article_cutoff=%s (>%dd) feed_cutoff=%s (>%dd)",
        article_cutoff, ARTICLE_EXPIRY_DAYS, feed_cutoff, FEED_EXPIRY_DAYS,
    )

    try:
        # ── 1. articles ───────────────────────────────────────────────────────
        art_result = _delete_collection_docs("articles", "fetched_at", article_cutoff)
        log.info(
            "cleanup: articles — deleted=%d errors=%d",
            art_result["deleted"], art_result["errors"],
        )

        # ── 2. feed_articles ──────────────────────────────────────────────────
        feed_result = _delete_collection_docs("feed_articles", "fetched_at", feed_cutoff)
        log.info(
            "cleanup: feed_articles — deleted=%d errors=%d",
            feed_result["deleted"], feed_result["errors"],
        )

        # ── 3. ai_insights ────────────────────────────────────────────────────
        # ai_insights documents use `created_at`.
        # Use the shorter of the two cutoffs so we don't leave orphaned insights.
        insight_cutoff = min(article_cutoff, feed_cutoff)
        insight_result = _delete_collection_docs("ai_insights", "created_at", insight_cutoff)

        # Sweep by known article / feed IDs that were just deleted
        all_deleted_ids = set(art_result["ids"]) | set(feed_result["ids"])
        already_deleted = set(insight_result["ids"])
        stragglers      = all_deleted_ids - already_deleted

        extra_deleted = 0
        for doc_id in stragglers:
            try:
                ref = db.collection("ai_insights").document(doc_id)
                if ref.get().exists:
                    ref.delete()
                    extra_deleted += 1
            except Exception as e:
                log.warning("cleanup: straggler ai_insights delete failed %s: %s", doc_id, e)

        insight_result["deleted"] += extra_deleted
        log.info(
            "cleanup: ai_insights — deleted=%d (incl %d stragglers) errors=%d",
            insight_result["deleted"], extra_deleted, insight_result["errors"],
        )

        # ── 4. insight_queue (done / failed entries) ──────────────────────────
        queue_deleted = 0
        try:
            for status in ("done", "failed"):
                q_docs = (
                    db.collection("insight_queue")
                      .where("status",     "==", status)
                      .where("queued_at",  "<",  insight_cutoff)
                      .stream()
                )
                for doc in q_docs:
                    try:
                        db.collection("insight_queue").document(doc.id).delete()
                        queue_deleted += 1
                    except Exception as e:
                        log.warning("cleanup: insight_queue delete failed %s: %s", doc.id, e)
        except Exception as e:
            log.error("cleanup: insight_queue sweep failed: %s", e)

        log.info("cleanup: insight_queue — deleted=%d", queue_deleted)

        summary = {
            "skipped":            False,
            "started_at":         started_at,
            "finished_at":        datetime.now(timezone.utc).isoformat(),
            "article_expiry_days": ARTICLE_EXPIRY_DAYS,
            "feed_expiry_days":    FEED_EXPIRY_DAYS,
            "article_cutoff":     article_cutoff,
            "feed_cutoff":        feed_cutoff,
            "articles":           {"deleted": art_result["deleted"],    "errors": art_result["errors"]},
            "feed":               {"deleted": feed_result["deleted"],   "errors": feed_result["errors"]},
            "ai_insights":        {"deleted": insight_result["deleted"],"errors": insight_result["errors"]},
            "insight_queue":      {"deleted": queue_deleted},
            "total_deleted": (
                art_result["deleted"]
                + feed_result["deleted"]
                + insight_result["deleted"]
                + queue_deleted
            ),
        }
        log.info(
            "cleanup: finished — total_deleted=%d articles=%d feed=%d insights=%d queue=%d",
            summary["total_deleted"],
            summary["articles"]["deleted"],
            summary["feed"]["deleted"],
            summary["ai_insights"]["deleted"],
            summary["insight_queue"]["deleted"],
        )
        return summary

    finally:
        with _cleanup_lock:
            _cleanup_running = False


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND SCHEDULER THREAD
# ══════════════════════════════════════════════════════════════════════════════

def _scheduler_loop() -> None:
    """
    Daemon thread: runs cleanup once on startup (after a short boot delay),
    then sleeps CLEANUP_INTERVAL_S seconds and repeats forever.
    """
    log.info("cleanup_scheduler: thread started — interval=%dh", CLEANUP_INTERVAL_S // 3600)

    # Brief delay so the app fully initialises before the first pass
    time.sleep(30)

    while True:
        try:
            result = run_cleanup_now()
            log.info("cleanup_scheduler: pass complete — %s", result)
        except Exception as e:
            log.error("cleanup_scheduler: unhandled error — %s", e)

        log.info("cleanup_scheduler: sleeping %dh until next run", CLEANUP_INTERVAL_S // 3600)
        time.sleep(CLEANUP_INTERVAL_S)


_scheduler_started = False
_scheduler_lock    = threading.Lock()


def start_cleanup_scheduler() -> None:
    """
    Start the daily cleanup daemon thread.
    Safe to call multiple times — only one thread is ever started.
    Call this inside your @app.on_event("startup") handler.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            log.info("cleanup_scheduler: already started — ignoring duplicate call")
            return
        _scheduler_started = True

    t = threading.Thread(
        target=_scheduler_loop,
        daemon=True,
        name="daily-cleanup-scheduler",
    )
    t.start()
    log.info("cleanup_scheduler: daemon thread launched")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN API ENDPOINT  (optional — include the router in main.py)
# ══════════════════════════════════════════════════════════════════════════════

cleanup_router = APIRouter()


@cleanup_router.post(
    "/cleanup",
    tags    = ["Admin"],
    summary = "Manually trigger article/feed cleanup — ADMIN ONLY",
)
def trigger_cleanup(payload: dict = Depends(require_admin)):
    """
    ADMIN ONLY — immediately run one cleanup pass without waiting for
    the next scheduled 24-hour window.

    Deletes articles and their AI insights older than 15 days, and
    feed items and their AI insights older than 15 days.
    """
    log.info("cleanup: manual trigger by admin=%s", get_uid(payload))
    result = run_cleanup_now()
    return result


@cleanup_router.get(
    "/cleanup/status",
    tags    = ["Admin"],
    summary = "Check cleanup scheduler status — ADMIN ONLY",
)
def cleanup_status(payload: dict = Depends(require_admin)):
    """ADMIN ONLY — check whether the cleanup scheduler is running."""
    return {
        "scheduler_started":    _scheduler_started,
        "cleanup_in_progress":  _cleanup_running,
        "article_expiry_days":  ARTICLE_EXPIRY_DAYS,
        "feed_expiry_days":     FEED_EXPIRY_DAYS,
        "interval_hours":       CLEANUP_INTERVAL_S // 3600,
    }