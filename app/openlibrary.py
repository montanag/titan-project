"""Thin client over the Open Library public API.

Responsibilities: build the right URLs, be a polite citizen (min spacing
between requests + exponential backoff on 429/5xx), and hand back parsed
JSON. It does no normalization and knows nothing about tenants or storage.
"""

from __future__ import annotations

import logging
import time

import requests

from app.config import settings

log = logging.getLogger(__name__)

# Fields we ask search.json to return up front (the "search-first" strategy).
_SEARCH_FIELDS = ",".join(
    ["key", "title", "author_name", "author_key", "first_publish_year", "cover_i", "subject"]
)


class OpenLibraryError(RuntimeError):
    pass


class OpenLibraryNotFound(OpenLibraryError):
    """A 404 — the resource does not exist (not retryable, not an outage)."""


class OpenLibraryClient:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": settings.user_agent})
        self._last_request_at = 0.0

    # -- public API ---------------------------------------------------------

    def search(self, kind: str, value: str, *, page: int) -> dict:
        """One page of search.json for an author name or subject."""
        if kind not in ("author", "subject"):
            raise ValueError(f"unsupported search kind: {kind!r}")
        params = {
            kind: value,
            "fields": _SEARCH_FIELDS,
            "page": page,
            "limit": settings.search_page_size,
        }
        return self._get_json(f"{settings.openlibrary_base_url}/search.json", params=params)

    def get_work(self, work_key: str) -> dict:
        """Full work record, e.g. work_key='/works/OL45883W'."""
        return self._get_json(f"{settings.openlibrary_base_url}{work_key}.json")

    def get_author(self, author_key: str) -> dict:
        """Author record, e.g. author_key='/authors/OL23919A'."""
        return self._get_json(f"{settings.openlibrary_base_url}{author_key}.json")

    def get_by_isbn(self, isbn: str) -> dict:
        """Edition record for an ISBN; raises OpenLibraryNotFound if unknown."""
        return self._get_json(f"{settings.openlibrary_base_url}/isbn/{isbn}.json")

    def cover_url(self, cover_id: int | None, size: str = "L") -> str | None:
        if not cover_id:
            return None
        return f"{settings.covers_base_url}/b/id/{cover_id}-{size}.jpg"

    # -- internals ----------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = settings.request_min_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get_json(self, url: str, *, params: dict | None = None) -> dict:
        """GET + parse JSON. Retries only transient failures (429/5xx/network);
        4xx is a definitive answer and is raised immediately (404 → NotFound)."""
        last_exc: Exception | None = None
        for attempt in range(settings.max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(url, params=params, timeout=settings.request_timeout_seconds)
                self._last_request_at = time.monotonic()
            except requests.RequestException as exc:
                # Network-level failure → transient, retry.
                last_exc = exc
                self._last_request_at = time.monotonic()
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = OpenLibraryError(f"{resp.status_code} from {resp.url}")
                elif resp.status_code == 404:
                    raise OpenLibraryNotFound(f"404 from {resp.url}")
                elif resp.status_code >= 400:
                    raise OpenLibraryError(f"{resp.status_code} from {resp.url}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise OpenLibraryError(f"invalid JSON from {resp.url}") from exc

            if attempt < settings.max_retries:
                backoff = settings.retry_backoff_base_seconds * (2**attempt)
                log.warning("OL request failed (%s); retry %d in %.1fs", last_exc, attempt + 1, backoff)
                time.sleep(backoff)
        raise OpenLibraryError(f"giving up on {url} after retries: {last_exc}")
