"""Domain dataclasses passed between ingestion stages.

Kept separate from the ORM models so the worker pipeline has no DB coupling
(makes the queue-agnostic seam clean and the stages unit-testable).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IngestJob:
    """A unit of ingestion work. This is what a queue would carry later."""

    tenant_id: str
    kind: str  # "author" | "subject"
    value: str


@dataclass
class BookRecord:
    """Canonical, normalized book ready to persist."""

    work_key: str
    title: str
    first_publish_year: int | None
    author_names: list[str]
    subjects: list[str]
    cover_url: str | None
    raw: dict


@dataclass
class IngestionResult:
    fetched: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)

    def record_error(self, work_key: str | None, message: str) -> None:
        self.failed += 1
        self.errors.append({"work_key": work_key, "error": message})
