"""FastAPI app — the tenant-scoped retrieval API (Tier 1).

Every catalog operation is scoped to a tenant identified by the ``X-Tenant``
header (the tenant's slug). The ``tenant`` dependency resolves it once and
404s on an unknown tenant, so no route can accidentally run unscoped.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app import repository as repo
from app.db import SessionLocal, init_db
from app.db_models import IngestionRun, Tenant
from app.schemas import (
    BookListOut,
    BookOut,
    IngestRequest,
    RunOut,
    TenantCreate,
    TenantOut,
)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Titan — Open Library Catalog", version="0.1.0", lifespan=lifespan)


# -- dependencies -----------------------------------------------------------

def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_tenant(
    db: Annotated[Session, Depends(get_db)],
    x_tenant: Annotated[str | None, Header(alias="X-Tenant")] = None,
) -> Tenant:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="Missing required 'X-Tenant' header")
    tenant = repo.get_tenant_by_slug(db, x_tenant)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Unknown tenant: {x_tenant!r}")
    return tenant


DbDep = Annotated[Session, Depends(get_db)]
TenantDep = Annotated[Tenant, Depends(get_tenant)]


# -- meta -------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(body: TenantCreate, db: DbDep) -> Tenant:
    existing = repo.get_tenant_by_slug(db, body.slug)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Tenant {body.slug!r} already exists")
    return repo.ensure_tenant(db, body.slug, body.name)


# -- catalog retrieval (Tier 1) ---------------------------------------------

@app.get("/books", response_model=BookListOut)
def list_books(
    tenant: TenantDep,
    db: DbDep,
    author: str | None = None,
    subject: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    q: Annotated[str | None, Query(description="keyword over title or author")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BookListOut:
    total, rows = repo.list_books(
        db,
        tenant.id,
        author=author,
        subject=subject,
        year_min=year_min,
        year_max=year_max,
        q=q,
        limit=limit,
        offset=offset,
    )
    return BookListOut(
        total=total, limit=limit, offset=offset, items=[BookOut.model_validate(r) for r in rows]
    )


@app.get("/books/{book_id}", response_model=BookOut)
def get_book(book_id: uuid.UUID, tenant: TenantDep, db: DbDep) -> BookOut:
    book = repo.get_book(db, tenant.id, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return BookOut.model_validate(book)


# -- activity log (Tier 1) --------------------------------------------------

@app.get("/activity", response_model=list[RunOut])
def activity_log(
    tenant: TenantDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunOut]:
    _total, runs = repo.list_runs(db, tenant.id, limit=limit, offset=offset)
    return [RunOut.model_validate(r) for r in runs]


# -- ingestion trigger (enqueues; a worker processes off the request path) --

@app.post("/ingest", response_model=RunOut, status_code=202)
def ingest(body: IngestRequest, tenant: TenantDep, db: DbDep) -> RunOut:
    """Enqueue an ingestion job and return immediately with a 'queued' run.

    The request does not block on Open Library; a background worker claims and
    processes the job. Poll GET /activity to watch it move queued→running→done.
    """
    run_id = repo.enqueue_run(db, tenant.id, body.kind, body.value)
    db.flush()
    run = db.get(IngestionRun, run_id)
    return RunOut.model_validate(run)
