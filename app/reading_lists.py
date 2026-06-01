"""Reading-list resolution.

A patron submits a list of identifiers — Open Library work IDs (``OL...W``)
and/or ISBNs. We resolve each to a work key and report it as *resolved* only
if that work exists in the requesting tenant's catalog. ISBNs are mapped to a
work key via Open Library's /isbn endpoint, then checked locally just like a
work ID. Anything we can't map, or that isn't in the tenant's catalog, comes
back as *unresolved*.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app import repository as repo
from app.openlibrary import OpenLibraryClient, OpenLibraryError

log = logging.getLogger(__name__)

_WORK_RE = re.compile(r"/?(?:works/)?(OL\d+W)$", re.IGNORECASE)


@dataclass
class Resolution:
    resolved: list[dict] = field(default_factory=list)  # {"input": raw, "work_key": wk}
    unresolved: list[str] = field(default_factory=list)  # raw identifiers

    @property
    def resolved_keys(self) -> list[str]:
        # Distinct, order-preserving.
        return list(dict.fromkeys(r["work_key"] for r in self.resolved))


def _normalize_isbn(raw: str) -> str | None:
    digits = raw.replace("-", "").replace(" ", "").upper()
    if re.fullmatch(r"\d{9}[\dX]", digits) or re.fullmatch(r"\d{13}", digits):
        return digits
    return None


def _to_work_key(raw: str, client: OpenLibraryClient) -> str | None:
    """Map a single identifier to a canonical work key, or None if unmappable."""
    s = raw.strip()
    m = _WORK_RE.fullmatch(s)
    if m:
        return f"/works/{m.group(1).upper()}"

    isbn = _normalize_isbn(s)
    if isbn:
        try:
            edition = client.get_by_isbn(isbn)
        except OpenLibraryError as exc:  # includes NotFound
            log.info("ISBN %s did not resolve: %s", isbn, exc)
            return None
        works = edition.get("works") or []
        if works and isinstance(works[0], dict):
            return works[0].get("key")
        return None

    return None  # unrecognized identifier format


def resolve_reading_list(
    session: Session,
    tenant_id,
    client: OpenLibraryClient,
    identifiers: list[str],
) -> Resolution:
    # Map each input to a candidate work key (may hit OL for ISBNs).
    mapping: dict[str, str | None] = {}
    for raw in identifiers:
        if raw not in mapping:  # de-dupe identical inputs, resolve each once
            mapping[raw] = _to_work_key(raw, client)

    candidates = [wk for wk in mapping.values() if wk]
    present = repo.work_keys_present(session, tenant_id, candidates)

    result = Resolution()
    for raw in identifiers:
        wk = mapping.get(raw)
        if wk and wk in present:
            result.resolved.append({"input": raw, "work_key": wk})
        else:
            result.unresolved.append(raw)
    return result
