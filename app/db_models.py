"""SQLAlchemy ORM models.

Tenant isolation lives at the storage boundary: every catalog row carries a
``tenant_id``, and the natural key ``(tenant_id, work_key)`` makes ingestion
idempotent per tenant. The author cache is intentionally *global* — Open
Library author records are the same for everyone, so caching them across
tenants maximizes the rate-limit savings.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    books: Mapped[list[BookBase]] = relationship(back_populates="tenant")


class BookBase(Base):
    __tablename__ = "books_base"
    __table_args__ = (UniqueConstraint("tenant_id", "work_key", name="uq_books_tenant_work"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    work_key: Mapped[str] = mapped_column(String(64), index=True)  # e.g. "/works/OL45883W"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tenant: Mapped[Tenant] = relationship(back_populates="books")

class BookVersion(Base):
    __tablename__ = "book_versions"
    __table_args__ = (UniqueConstraint("book_id", "created_at", name="uq_book_versions_book_created"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    book_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("books_base.id", ondelete="CASCADE"), index=True
    )

    title: Mapped[str] = mapped_column(Text)
    first_publish_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_names: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    subjects: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Raw normalized snapshot — cheap insurance for re-normalize and Tier 3 versioning.
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthorCache(Base):
    """Global cache of resolved Open Library authors (not tenant-scoped)."""

    __tablename__ = "authors_cache"

    author_key: Mapped[str] = mapped_column(String(64), primary_key=True)  # "/authors/OL23919A"
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReadingListSubmission(Base):
    """A patron's reading list. Holds NO plaintext PII — only the irreversible
    ``patron_hash`` (for dedup) and masked display strings."""

    __tablename__ = "reading_list_submissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    patron_hash: Mapped[str] = mapped_column(String(64), index=True)  # HMAC-SHA256 hex
    name_masked: Mapped[str] = mapped_column(Text)
    email_masked: Mapped[str] = mapped_column(Text)

    requested: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    resolved: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    unresolved: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionRun(Base):
    """One row per ingestion operation — backs the Activity Log endpoint."""

    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    kind: Mapped[str] = mapped_column(String(16))  # "author" | "subject"
    value: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed

    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSONB, default=list)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
