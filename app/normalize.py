"""Gap detection + normalization into canonical base + version records.

This is the brains of the "search-first" strategy: a search.json doc usually
has everything, so we only flag the fields it's missing and let the worker
spend follow-up requests selectively.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.types import BookBaseRecord, BookVersionRecord


@dataclass(frozen=True)
class Gaps:
    needs_work: bool  # title / subjects / year missing → fetch /works/{key}
    needs_authors: bool  # have author keys but no names → fetch /authors/{key}


def author_key_path(bare_or_path: str) -> str:
    """Search returns bare author ids ('OL..A'); the API wants '/authors/OL..A'."""
    return bare_or_path if bare_or_path.startswith("/authors/") else f"/authors/{bare_or_path}"


def detect_gaps(doc: dict) -> Gaps:
    missing_core = not doc.get("title") or not doc.get("subject") or not doc.get("first_publish_year")
    missing_names = not doc.get("author_name") and bool(doc.get("author_key"))
    return Gaps(needs_work=missing_core, needs_authors=missing_names)


def _first_year(value) -> int | None:
    """Coerce a publish year/date into an int, tolerating OL's messy formats."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        for token in value.replace("-", " ").replace("/", " ").split():
            if token.isdigit() and len(token) == 4:
                return int(token)
    return None


def build_records(
    doc: dict,
    *,
    work: dict | None = None,
    author_names: list[str] | None = None,
    cover_url: str | None = None,
) -> tuple[BookBaseRecord, BookVersionRecord]:
    """Merge a search doc with optional enrichment into a canonical record.

    Precedence: search values first (they're already flattened), then fall
    back to the authoritative /works payload for anything still missing.
    """
    work = work or {}

    title = doc.get("title") or work.get("title") or "(untitled)"

    year = _first_year(doc.get("first_publish_year"))
    if year is None:
        year = _first_year(work.get("first_publish_date"))

    # Subjects: search 'subject' is already a flat list of strings; the works
    # endpoint also exposes 'subjects' as a flat list.
    subjects = doc.get("subject") or work.get("subjects") or []
    subjects = sorted({s for s in subjects if isinstance(s, str)})

    names = author_names if author_names is not None else (doc.get("author_name") or [])

    # Cover: prefer the precomputed search cover; else first cover id on the work.
    if cover_url is None and work.get("covers"):
        first = next((c for c in work["covers"] if isinstance(c, int) and c > 0), None)
        if first:
            cover_url = f"https://covers.openlibrary.org/b/id/{first}-L.jpg"

    return BookBaseRecord(work_key=doc["key"]), BookVersionRecord(
        title=title,
        first_publish_year=year,
        author_names=names,
        subjects=subjects,
        cover_url=cover_url,
        raw={"search": doc, "work": work or None},
    )
