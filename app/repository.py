"""Storage operations. The only layer that touches tenant_id and the DB.

Tenant scoping is enforced here: callers pass a tenant_id and every write is
stamped with it; the (tenant_id, work_key) upsert keeps ingestion idempotent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from app.db_models import (
    AuthorCache,
    BookBase,
    BookVersion,
    IngestionRun,
    ReadingListSubmission,
    Tenant,
)
from app.types import BookBaseRecord, BookVersionRecord, IngestionResult


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


# -- books: identity + version tracking (Tier 3) ----------------------------
#
# A book's identity (BookBase) is separated from its metadata snapshots
# (BookVersion). Each ingestion compares the freshly-normalized record against
# the latest stored version and appends a NEW version only when something
# changed — so history is preserved and we never overwrite. A regression (a
# field that previously had a value and is now missing) is just another change:
# it produces a new version recording the now-empty value, rather than being
# silently dropped or crashing.

# Metadata fields that define a version; a change in any of them is a new version.
_VERSIONED_FIELDS = ("title", "first_publish_year", "author_names", "subjects", "cover_url")


def upsert_book_base(session: Session, tenant_id: uuid.UUID, record: BookBaseRecord) -> uuid.UUID:
    """Ensure the (tenant, work_key) identity row exists and return its id.

    Idempotent: INSERT ... ON CONFLICT DO NOTHING, then read the id back (the
    RETURNING clause yields nothing on conflict, so we fall back to a select)."""
    book_id = session.scalar(
        insert(BookBase)
        .values(tenant_id=tenant_id, work_key=record.work_key)
        .on_conflict_do_nothing(constraint="uq_books_tenant_work")
        .returning(BookBase.id)
    )
    if book_id is None:  # row already existed
        book_id = session.scalar(
            select(BookBase.id).where(
                BookBase.tenant_id == tenant_id, BookBase.work_key == record.work_key
            )
        )
    return book_id


def get_latest_version(session: Session, book_id: uuid.UUID) -> BookVersion | None:
    return session.scalar(
        select(BookVersion)
        .where(BookVersion.book_id == book_id)
        .order_by(BookVersion.created_at.desc())
        .limit(1)
    )


def diff_versions(prev, curr) -> dict[str, dict]:
    """Field-level changes between two version-like objects (ORM rows or records).

    Returns ``{field: {"from": old, "to": new}}`` for each differing versioned
    field. Lists are compared by value; None vs a value (a regression, or a
    newly-populated field) shows up like any other change.
    """
    changes: dict[str, dict] = {}
    for field_name in _VERSIONED_FIELDS:
        old = getattr(prev, field_name)
        new = getattr(curr, field_name)
        if isinstance(old, (list, tuple)) or isinstance(new, (list, tuple)):
            old = list(old or [])
            new = list(new or [])
        if old != new:
            changes[field_name] = {"from": old, "to": new}
    return changes


def add_version_if_changed(
    session: Session, book_id: uuid.UUID, record: BookVersionRecord
) -> BookVersion | None:
    """Append a new version iff the metadata differs from the latest one.

    Returns the newly-created version, or None when nothing changed (no-op)."""
    latest = get_latest_version(session, book_id)
    if latest is not None and not diff_versions(latest, record):
        return None

    version = BookVersion(
        book_id=book_id,
        title=record.title,
        first_publish_year=record.first_publish_year,
        author_names=record.author_names,
        subjects=record.subjects,
        cover_url=record.cover_url,
        raw=record.raw,
    )
    session.add(version)
    session.flush()
    return version


def store_book(
    session: Session,
    tenant_id: uuid.UUID,
    base_record: BookBaseRecord,
    version_record: BookVersionRecord,
) -> BookVersion | None:
    """Ingestion entry point: ensure identity, then version-on-change.

    Returns the new version (book newly seen, or metadata changed) or None if
    the record matched the current version exactly."""
    book_id = upsert_book_base(session, tenant_id, base_record)
    return add_version_if_changed(session, book_id, version_record)


# -- book queries (retrieval API) -------------------------------------------
#
# The catalog views a book as its current (latest) version. We pick the latest
# version per book with a "no newer version exists" predicate, which keeps the
# BookVersion ORM columns (and their ARRAY operators) intact for filtering.

def _is_latest_version():
    newer = aliased(BookVersion)
    return ~exists(
        select(newer.id).where(
            newer.book_id == BookVersion.book_id,
            newer.created_at > BookVersion.created_at,
        )
    )


@dataclass
class BookView:
    """Flattened current-version view of a book for the retrieval API."""

    id: uuid.UUID
    work_key: str
    title: str
    first_publish_year: int | None
    author_names: list[str]
    subjects: list[str]
    cover_url: str | None
    created_at: datetime  # when the book was first ingested
    updated_at: datetime  # when the current version was recorded


def _to_view(base: BookBase, version: BookVersion) -> BookView:
    return BookView(
        id=base.id,
        work_key=base.work_key,
        title=version.title,
        first_publish_year=version.first_publish_year,
        author_names=list(version.author_names),
        subjects=list(version.subjects),
        cover_url=version.cover_url,
        created_at=base.created_at,
        updated_at=version.created_at,
    )

@dataclass
class BookHistoryView:
    id: uuid.UUID
    work_key: str
    versions: list[BookView]


def _apply_book_filters(
    stmt: Select,
    *,
    author: str | None,
    subject: str | None,
    year_min: int | None,
    year_max: int | None,
    q: str | None,
) -> Select:
    """Compose the optional filters over the current version. Filters are
    precise; `q` is fuzzy."""
    if author:
        # Forgiving partial match across the author list.
        joined = func.array_to_string(BookVersion.author_names, " ")
        stmt = stmt.where(joined.ilike(f"%{author}%"))
    if subject:
        # Exact membership in the subjects array (subjects @> ARRAY[subject]).
        stmt = stmt.where(BookVersion.subjects.contains([subject]))
    if year_min is not None:
        stmt = stmt.where(BookVersion.first_publish_year >= year_min)
    if year_max is not None:
        stmt = stmt.where(BookVersion.first_publish_year <= year_max)
    if q:
        joined = func.array_to_string(BookVersion.author_names, " ")
        stmt = stmt.where(or_(BookVersion.title.ilike(f"%{q}%"), joined.ilike(f"%{q}%")))
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
) -> tuple[int, list[BookView]]:
    base = (
        select(BookBase, BookVersion)
        .join(BookVersion, BookVersion.book_id == BookBase.id)
        .where(BookBase.tenant_id == tenant_id, _is_latest_version())
    )
    base = _apply_book_filters(
        base, author=author, subject=subject, year_min=year_min, year_max=year_max, q=q
    )
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(
        base.order_by(BookVersion.title).limit(limit).offset(offset)
    ).all()
    return total, [_to_view(b, v) for b, v in rows]


def get_book(session: Session, tenant_id: uuid.UUID, book_id: uuid.UUID) -> BookView | None:
    row = session.execute(
        select(BookBase, BookVersion)
        .join(BookVersion, BookVersion.book_id == BookBase.id)
        .where(BookBase.id == book_id, BookBase.tenant_id == tenant_id, _is_latest_version())
    ).first()
    return _to_view(row[0], row[1]) if row else None


def list_book_versions(
    session: Session, tenant_id: uuid.UUID, book_id: uuid.UUID
) -> list[BookVersion]:
    """Full version history for a book (oldest → newest), tenant-scoped.

    Backs a version-history endpoint; pair consecutive versions with
    ``diff_versions`` to show what changed between them."""
    return list(
        session.scalars(
            select(BookVersion)
            .join(BookBase, BookBase.id == BookVersion.book_id)
            .where(BookBase.id == book_id, BookBase.tenant_id == tenant_id)
            .order_by(BookVersion.created_at.asc())
        ).all()
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
    """Subset of the given work keys that exist in this tenant's catalog.

    Presence is an identity question, so it queries BookBase — a book counts as
    present as soon as it has been ingested, independent of its versions."""
    if not work_keys:
        return set()
    rows = session.scalars(
        select(BookBase.work_key).where(
            BookBase.tenant_id == tenant_id, BookBase.work_key.in_(work_keys)
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
