"""
celery_app.py — Celery application + Beat schedule
===================================================
- Broker  : Redis (REDIS_URL env var from Render)
- Backend : Redis (same URL, different DB index)
- Schedule: Daily newsletter at 2:00 PM IST (08:30 UTC)

IST = UTC+5:30  →  14:00 IST = 08:30 UTC
"""

import os
from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Use DB 1 for results so it doesn't collide with the broker (DB 0)
REDIS_BACKEND = REDIS_URL.rstrip("/").rstrip("0") + "1" \
    if REDIS_URL.endswith("/0") else REDIS_URL + "/1"

celery = Celery(
    "steami_newsletter",
    broker=REDIS_URL,
    backend=REDIS_BACKEND,
    include=["tasks"],          # tasks.py lives next to this file
)

celery.conf.update(
    timezone="UTC",
    enable_utc=True,

    # ── Beat schedule ──────────────────────────────────────────────
    beat_schedule={
        "daily-newsletter-2pm-ist": {
            "task":     "tasks.send_daily_newsletter",
            "schedule": crontab(hour=8, minute=30),   # 08:30 UTC = 14:00 IST
            "args":     (5,),                          # limit: top-5 articles
        },
    },

    # ── Reliability settings ───────────────────────────────────────
    task_acks_late=True,                # re-queue on worker crash
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,       # one task at a time (newsletter is heavy)
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=86400,               # keep results 24 h
)
