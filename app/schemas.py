"""Pydantic models for API request/response bodies."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str


class TenantCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=64)
    name: str | None = None


class BookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    work_key: str
    title: str
    first_publish_year: int | None
    author_names: list[str]
    subjects: list[str]
    cover_url: str | None
    created_at: datetime
    updated_at: datetime


class BookListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[BookOut]


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    value: str
    status: str
    fetched_count: int
    succeeded_count: int
    failed_count: int
    errors: list
    requested_at: datetime
    finished_at: datetime | None


class IngestRequest(BaseModel):
    kind: str = Field(pattern="^(author|subject)$")
    value: str = Field(min_length=1)


# -- reading lists (PII) ----------------------------------------------------

class ReadingListSubmit(BaseModel):
    """Incoming patron submission. The plaintext name/email are used only to
    derive the hash + masks and are never persisted."""

    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    books: list[str] = Field(min_length=1, description="Open Library work IDs and/or ISBNs")


class ResolvedBook(BaseModel):
    input: str
    work_key: str


class ReadingListResult(BaseModel):
    """Response to a submission — confirms what resolved and what didn't."""

    id: uuid.UUID
    patron_ref: str = Field(description="stable per-patron reference (hash prefix)")
    is_returning_patron: bool
    requested_count: int
    resolved: list[ResolvedBook]
    unresolved: list[str]


class SubmissionOut(BaseModel):
    """Staff-facing view of a stored submission — masked PII only."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patron_hash: str
    name_masked: str
    email_masked: str
    requested: list[str]
    resolved: list[str]
    unresolved: list[str]
    created_at: datetime
