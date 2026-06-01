"""Catalog freshness scheduler.

Keeps the catalog current without manual intervention: periodically it looks
at every distinct ingestion job ((tenant, kind, value) ever run) and, if that
job's most recent run finished longer ago than the freshness interval,
re-enqueues it. The worker then re-fetches and idempotently upserts, so
changed metadata flows in over time. Run as a separate process:

    uv run python -m app.scheduler

Dedup is implicit: once re-enqueued, the job's latest run is queued/running,
so it won't be enqueued again until it finishes and ages out.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import init_db, session_scope
from app.repository import enqueue_run, latest_run_per_job

log = logging.getLogger("titan.scheduler")

_ACTIVE = {"queued", "running"}
_running = True


def _stop(*_: object) -> None:
    global _running
    _running = False


def enqueue_stale(now: datetime) -> int:
    """Re-enqueue every job whose last run is older than the freshness window."""
    cutoff = now - timedelta(seconds=settings.freshness_interval_seconds)
    enqueued = 0
    with session_scope() as session:
        for run in latest_run_per_job(session):
            if run.status in _ACTIVE:
                continue  # already queued or being processed — don't pile on
            if run.finished_at is not None and run.finished_at > cutoff:
                continue  # still fresh
            enqueue_run(session, run.tenant_id, run.kind, run.value)
            enqueued += 1
            log.info("re-enqueued stale job: %s=%s (tenant %s)", run.kind, run.value, run.tenant_id)
    return enqueued


def run_scheduler() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    init_db()
    log.info(
        "scheduler started; tick=%.0fs freshness=%.0fs",
        settings.scheduler_tick_seconds,
        settings.freshness_interval_seconds,
    )

    while _running:
        try:
            n = enqueue_stale(datetime.now(timezone.utc))
            if n:
                log.info("re-enqueued %d stale job(s)", n)
        except Exception:  # noqa: BLE001 — a bad tick shouldn't kill the scheduler
            log.exception("scheduler tick failed")
        # Sleep in short slices so shutdown is responsive.
        slept = 0.0
        while _running and slept < settings.scheduler_tick_seconds:
            time.sleep(0.5)
            slept += 0.5

    log.info("scheduler stopped")


if __name__ == "__main__":
    run_scheduler()
