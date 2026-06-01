"""PII handling for patron data.

We never persist a patron's name or email in plaintext. Instead:

* ``patron_hash`` — an HMAC-SHA256 of the *normalized* email keyed by a
  server-side pepper. It is deterministic (same email → same hash, so we can
  dedupe a returning patron) but irreversible without the pepper, so a leaked
  database can't be turned back into email addresses via a rainbow table.
* masked display strings — a human-readable hint for staff (``j***@g***.com``,
  ``U*** L***``) that reveals neither the full name nor the full address.
"""

from __future__ import annotations

import hashlib
import hmac

from app.config import settings


def normalize_email(email: str) -> str:
    """Canonicalize before hashing so trivial variants dedupe together."""
    return email.strip().lower()


def patron_hash(email: str) -> str:
    """Stable, irreversible dedup key for a patron's email."""
    normalized = normalize_email(email).encode("utf-8")
    return hmac.new(settings.pii_pepper.encode("utf-8"), normalized, hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    """'jane.doe@gmail.com' -> 'j***@g***.com' (keeps shape, hides content)."""
    email = email.strip()
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    name, _, tld = domain.rpartition(".")
    local_masked = (local[:1] or "*") + "***"
    domain_masked = (name[:1] or "*") + "***"
    return f"{local_masked}@{domain_masked}.{tld}" if tld else f"{local_masked}@{domain_masked}"


def mask_name(name: str) -> str:
    """'Ursula K. Le Guin' -> 'U*** K*** L*** G***'."""
    parts = [p for p in name.strip().split() if p]
    return " ".join((p[:1] + "***") for p in parts) or "***"
