"""The ingestion worker — the queue-agnostic seam.

``run_ingestion`` is the *worker body*: it takes a job, runs the
resolve → enrich → normalize → store → log pipeline, and returns a result.
It knows nothing about *how* it was invoked. Today the API/script calls it
inline; later a background worker pops a job off a queue and calls the exact
same function — only the caller changes.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app import repository as repo
from app.config import settings
from app.normalize import author_key_path, build_record, detect_gaps
from app.openlibrary import OpenLibraryClient, OpenLibraryError
from app.types import IngestionResult, IngestJob

log = logging.getLogger(__name__)


def _resolve_author_names(
    session: Session,
    client: OpenLibraryClient,
    author_keys: list[str],
    run_cache: dict[str, str | None],
) -> list[str]:
    """Resolve bare author ids → names, using the run-local + DB caches first."""
    names: list[str] = []
    for bare in author_keys:
        key = author_key_path(bare)
        if key in run_cache:
            name = run_cache[key]
        else:
            name = repo.get_cached_author_name(session, key)
            if name is None:
                data = client.get_author(key)
                name = data.get("name")
                repo.upsert_author_cache(session, key, name, data)
            run_cache[key] = name
        if name:
            names.append(name)
    return names


def run_ingestion(
    run_id,
    job: IngestJob,
    *,
    session: Session,
    client: OpenLibraryClient,
    max_pages: int,
) -> IngestionResult:
    """Execute one already-created ingestion run against the live OL API.

    The run row (status 'running') is the durable job; this fills in counts
    and flips it to a terminal status. It does NOT create the run — the queue
    (enqueue + claim) owns the run's lifecycle up to this point.
    """
    result = IngestionResult()
    author_cache: dict[str, str | None] = {}
    status = "succeeded"

    try:
        for page in range(1, max_pages + 1):
            payload = client.search(job.kind, job.value, page=page)
            docs = payload.get("docs", [])
            if not docs:
                break
            result.fetched += len(docs)

            for doc in docs:
                work_key = doc.get("key")
                try:
                    if not work_key:
                        raise ValueError("search doc missing 'key'")

                    gaps = detect_gaps(doc)
                    work = client.get_work(work_key) if gaps.needs_work else None

                    names = None
                    if gaps.needs_authors:
                        names = _resolve_author_names(
                            session, client, doc.get("author_key", []), author_cache
                        )

                    cover_url = client.cover_url(doc.get("cover_i"))
                    record = build_record(doc, work=work, author_names=names, cover_url=cover_url)
                    repo.upsert_book(session, job.tenant_id, record)
                    result.succeeded += 1
                except (OpenLibraryError, ValueError, KeyError) as exc:
                    log.warning("failed to ingest %s: %s", work_key, exc)
                    result.record_error(work_key, str(exc))

            # Last page (fewer results than a full page) → stop.
            if len(docs) < settings.search_page_size:
                break
    except OpenLibraryError as exc:
        # A hard resolve failure (search itself fails after retries).
        log.error("ingestion resolve failed for %s=%s: %s", job.kind, job.value, exc)
        result.record_error(None, f"resolve failed: {exc}")
        status = "failed" if result.succeeded == 0 else "succeeded"

    repo.finalize_run(session, run_id, result, status)
    return result


def run_job_inline(
    job: IngestJob,
    *,
    session: Session,
    client: OpenLibraryClient,
    max_pages: int,
) -> IngestionResult:
    """Enqueue a job and run it synchronously in the caller's session.

    Convenience for the CLI/dev path and tests — exercises the full
    enqueue → claim → run lifecycle without a separate worker process.
    """
    run_id = repo.enqueue_run(session, job.tenant_id, job.kind, job.value)
    repo.mark_running(session, run_id)
    return run_ingestion(run_id, job, session=session, client=client, max_pages=max_pages)
