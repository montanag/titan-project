"""Background ingestion worker.

Polls the ingestion_runs queue, claims queued jobs (FOR UPDATE SKIP LOCKED),
and runs them off the request path. Run as a separate process:

    uv run python -m app.worker

Multiple instances can run concurrently — SKIP LOCKED guarantees each job is
claimed by exactly one worker. Stop with Ctrl-C.
"""

from __future__ import annotations

import logging
import signal
import time

from app.config import settings
from app.db import init_db, session_scope
from app.ingestion import run_ingestion
from app.openlibrary import OpenLibraryClient
from app.repository import claim_next_run, finalize_run
from app.types import IngestionResult, IngestJob

log = logging.getLogger("titan.worker")

_running = True


def _stop(*_: object) -> None:
    global _running
    log.info("shutdown signal received; finishing current job then exiting")
    _running = False


def process_next(client: OpenLibraryClient) -> bool:
    """Claim and process one job. Returns True if a job was handled."""
    # 1) Claim in a short transaction so the row lock is released immediately.
    with session_scope() as session:
        run = claim_next_run(session)
        if run is None:
            return False
        run_id = run.id
        job = IngestJob(tenant_id=run.tenant_id, kind=run.kind, value=run.value)

    log.info("processing run %s: %s=%s", run_id, job.kind, job.value)

    # 2) Do the slow work in its own transaction (no lock held during HTTP).
    try:
        with session_scope() as session:
            result = run_ingestion(
                run_id, job, session=session, client=client, max_pages=settings.max_pages_per_run
            )
        log.info(
            "run %s done: fetched=%d ok=%d failed=%d",
            run_id, result.fetched, result.succeeded, result.failed,
        )
    except Exception as exc:  # noqa: BLE001 — never let one job kill the worker
        log.exception("run %s crashed; marking failed", run_id)
        with session_scope() as session:
            failed = IngestionResult()
            failed.record_error(None, f"worker crash: {exc}")
            finalize_run(session, run_id, failed, "failed")
    return True


def run_worker() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    init_db()
    client = OpenLibraryClient()
    log.info("worker started; polling every %.1fs", settings.worker_poll_interval_seconds)

    while _running:
        # Drain all available jobs, then idle.
        if not process_next(client):
            time.sleep(settings.worker_poll_interval_seconds)

    log.info("worker stopped")


if __name__ == "__main__":
    run_worker()
