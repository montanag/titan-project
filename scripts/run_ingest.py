"""Exercise the ingestion pipeline against the live Open Library API.

Usage:
    uv run python -m scripts.run_ingest <tenant-slug> <author|subject> "<value>"

Example:
    uv run python -m scripts.run_ingest demo author "Charlotte Lamb"
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import func, select

from app.db import init_db, session_scope
from app.db_models import Book, IngestionRun
from app.ingestion import run_job_inline
from app.openlibrary import OpenLibraryClient
from app.repository import ensure_tenant
from app.types import IngestJob


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(argv) != 4 or argv[2] not in ("author", "subject"):
        print(__doc__)
        return 2

    slug, kind, value = argv[1], argv[2], argv[3]
    init_db()
    client = OpenLibraryClient()

    with session_scope() as session:
        tenant = ensure_tenant(session, slug)
        tenant_id = tenant.id
        job = IngestJob(tenant_id=tenant_id, kind=kind, value=value)
        result = run_job_inline(job, session=session, client=client, max_pages=2)

    print("\n=== ingestion result ===")
    print(f"fetched={result.fetched} succeeded={result.succeeded} failed={result.failed}")
    if result.errors:
        print(f"errors (first 3): {result.errors[:3]}")

    # Read back what we stored, scoped to this tenant.
    with session_scope() as session:
        total = session.scalar(
            select(func.count()).select_from(Book).where(Book.tenant_id == tenant_id)
        )
        sample = session.scalars(
            select(Book).where(Book.tenant_id == tenant_id).order_by(Book.title).limit(3)
        ).all()
        runs = session.scalars(
            select(IngestionRun)
            .where(IngestionRun.tenant_id == tenant_id)
            .order_by(IngestionRun.requested_at.desc())
            .limit(1)
        ).all()

    print(f"\n=== stored books for tenant '{slug}': {total} ===")
    for b in sample:
        print(f"  • {b.title} ({b.first_publish_year}) — {', '.join(b.author_names) or '?'}")
        print(f"    subjects={len(b.subjects)} cover={'yes' if b.cover_url else 'no'} key={b.work_key}")

    for r in runs:
        print(
            f"\n=== latest activity log row ===\n"
            f"  {r.kind}={r.value} status={r.status} "
            f"fetched={r.fetched_count} ok={r.succeeded_count} failed={r.failed_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
