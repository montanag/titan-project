"""Storage operations. The only layer that touches tenant_id and the DB.

Tenant scoping is enforced here: callers pass a tenant_id and every write is
stamped with it; the (tenant_id, work_key) upsert keeps ingestion idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db_models import AuthorCache, Book, IngestionRun, Tenant
from app.types import BookRecord, IngestionResult


# -- tenants ----------------------------------------------------------------

def ensure_tenant(session: Session, slug: str, name: str | None = None) -> Tenant:
    tenant = session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        tenant = Tenant(slug=slug, name=name or slug)
        session.add(tenant)
        session.flush()
    return tenant


# -- author cache (global) --------------------------------------------------

def get_cached_author_name(session: Session, author_key: str) -> str | None:
    return session.scalar(select(AuthorCache.name).where(AuthorCache.author_key == author_key))


def upsert_author_cache(session: Session, author_key: str, name: str | None, raw: dict) -> None:
    stmt = insert(AuthorCache).values(author_key=author_key, name=name, raw=raw)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AuthorCache.author_key],
        set_={"name": name, "raw": raw, "fetched_at": datetime.now(timezone.utc)},
    )
    session.execute(stmt)


# -- books ------------------------------------------------------------------

def upsert_book(session: Session, tenant_id: uuid.UUID, record: BookRecord) -> None:
    stmt = insert(Book).values(
        tenant_id=tenant_id,
        work_key=record.work_key,
        title=record.title,
        first_publish_year=record.first_publish_year,
        author_names=record.author_names,
        subjects=record.subjects,
        cover_url=record.cover_url,
        raw=record.raw,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_books_tenant_work",
        set_={
            "title": stmt.excluded.title,
            "first_publish_year": stmt.excluded.first_publish_year,
            "author_names": stmt.excluded.author_names,
            "subjects": stmt.excluded.subjects,
            "cover_url": stmt.excluded.cover_url,
            "raw": stmt.excluded.raw,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    session.execute(stmt)


# -- ingestion runs (activity log) ------------------------------------------

def create_run(session: Session, tenant_id: uuid.UUID, kind: str, value: str) -> uuid.UUID:
    run = IngestionRun(tenant_id=tenant_id, kind=kind, value=value, status="running")
    session.add(run)
    session.flush()
    return run.id


def finalize_run(session: Session, run_id: uuid.UUID, result: IngestionResult, status: str) -> None:
    run = session.get(IngestionRun, run_id)
    if run is None:
        return
    run.status = status
    run.fetched_count = result.fetched
    run.succeeded_count = result.succeeded
    run.failed_count = result.failed
    run.errors = result.errors
    run.finished_at = datetime.now(timezone.utc)
