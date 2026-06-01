"""Storage operations. The only layer that touches tenant_id and the DB.

Tenant scoping is enforced here: callers pass a tenant_id and every write is
stamped with it; the (tenant_id, work_key) upsert keeps ingestion idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db_models import (
    AuthorCache,
    Book,
    IngestionRun,
    ReadingListSubmission,
    Tenant,
)
from app.types import BookRecord, IngestionResult


# -- tenants ----------------------------------------------------------------

def ensure_tenant(session: Session, slug: str, name: str | None = None) -> Tenant:
    tenant = get_tenant_by_slug(session, slug)
    if tenant is None:
        tenant = Tenant(slug=slug, name=name or slug)
        session.add(tenant)
        session.flush()
    return tenant


def get_tenant_by_slug(session: Session, slug: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.slug == slug))


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


# -- book queries (retrieval API) -------------------------------------------

def _apply_book_filters(
    stmt: Select,
    *,
    author: str | None,
    subject: str | None,
    year_min: int | None,
    year_max: int | None,
    q: str | None,
) -> Select:
    """Compose the optional filters. Filters are precise; `q` is fuzzy."""
    if author:
        # Forgiving partial match across the author list.
        joined = func.array_to_string(Book.author_names, " ")
        stmt = stmt.where(joined.ilike(f"%{author}%"))
    if subject:
        # Exact membership in the subjects array (subjects @> ARRAY[subject]).
        stmt = stmt.where(Book.subjects.contains([subject]))
    if year_min is not None:
        stmt = stmt.where(Book.first_publish_year >= year_min)
    if year_max is not None:
        stmt = stmt.where(Book.first_publish_year <= year_max)
    if q:
        joined = func.array_to_string(Book.author_names, " ")
        stmt = stmt.where(or_(Book.title.ilike(f"%{q}%"), joined.ilike(f"%{q}%")))
    return stmt


def list_books(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    author: str | None = None,
    subject: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[int, list[Book]]:
    base = select(Book).where(Book.tenant_id == tenant_id)
    base = _apply_book_filters(
        base, author=author, subject=subject, year_min=year_min, year_max=year_max, q=q
    )
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(
        base.order_by(Book.title).limit(limit).offset(offset)
    ).all()
    return total, list(rows)


def get_book(session: Session, tenant_id: uuid.UUID, book_id: uuid.UUID) -> Book | None:
    return session.scalar(
        select(Book).where(Book.id == book_id, Book.tenant_id == tenant_id)
    )


# -- ingestion runs (activity log) ------------------------------------------

def list_runs(
    session: Session, tenant_id: uuid.UUID, *, limit: int = 25, offset: int = 0
) -> tuple[int, list[IngestionRun]]:
    base = select(IngestionRun).where(IngestionRun.tenant_id == tenant_id)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(
        base.order_by(IngestionRun.requested_at.desc()).limit(limit).offset(offset)
    ).all()
    return total, list(rows)


def latest_run_per_job(session: Session) -> list[IngestionRun]:
    """Most recent run for each distinct (tenant, kind, value) job, all tenants.

    Backs the freshness scheduler: it decides what to re-sync by looking at
    each job's latest run (its status + finished_at).
    """
    return list(
        session.scalars(
            select(IngestionRun)
            .distinct(IngestionRun.tenant_id, IngestionRun.kind, IngestionRun.value)
            .order_by(
                IngestionRun.tenant_id,
                IngestionRun.kind,
                IngestionRun.value,
                IngestionRun.requested_at.desc(),
            )
        ).all()
    )


# -- reading list submissions (PII) -----------------------------------------

def create_submission(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    patron_hash: str,
    name_masked: str,
    email_masked: str,
    requested: list[str],
    resolved: list[str],
    unresolved: list[str],
) -> ReadingListSubmission:
    sub = ReadingListSubmission(
        tenant_id=tenant_id,
        patron_hash=patron_hash,
        name_masked=name_masked,
        email_masked=email_masked,
        requested=requested,
        resolved=resolved,
        unresolved=unresolved,
    )
    session.add(sub)
    session.flush()
    return sub


def count_submissions_for_patron(
    session: Session, tenant_id: uuid.UUID, patron_hash: str
) -> int:
    return session.scalar(
        select(func.count())
        .select_from(ReadingListSubmission)
        .where(
            ReadingListSubmission.tenant_id == tenant_id,
            ReadingListSubmission.patron_hash == patron_hash,
        )
    ) or 0


def list_submissions(
    session: Session, tenant_id: uuid.UUID, *, limit: int = 25, offset: int = 0
) -> tuple[int, list[ReadingListSubmission]]:
    base = select(ReadingListSubmission).where(ReadingListSubmission.tenant_id == tenant_id)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(
        base.order_by(ReadingListSubmission.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return total, list(rows)


def work_keys_present(
    session: Session, tenant_id: uuid.UUID, work_keys: list[str]
) -> set[str]:
    """Subset of the given work keys that exist in this tenant's catalog."""
    if not work_keys:
        return set()
    rows = session.scalars(
        select(Book.work_key).where(
            Book.tenant_id == tenant_id, Book.work_key.in_(work_keys)
        )
    ).all()
    return set(rows)



def enqueue_run(session: Session, tenant_id: uuid.UUID, kind: str, value: str) -> uuid.UUID:
    """Create a job in 'queued' state for a worker to pick up."""
    run = IngestionRun(tenant_id=tenant_id, kind=kind, value=value, status="queued")
    session.add(run)
    session.flush()
    return run.id


def mark_running(session: Session, run_id: uuid.UUID) -> None:
    run = session.get(IngestionRun, run_id)
    if run is not None:
        run.status = "running"


def claim_next_run(session: Session) -> IngestionRun | None:
    """Atomically claim the oldest queued job and flip it to 'running'.

    FOR UPDATE SKIP LOCKED lets multiple workers claim distinct jobs without
    blocking each other. The caller must commit to release the row lock before
    doing the (slow) ingestion work — never hold the lock across HTTP calls.
    """
    run = session.scalar(
        select(IngestionRun)
        .where(IngestionRun.status == "queued")
        .order_by(IngestionRun.requested_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is not None:
        run.status = "running"
    return run


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
