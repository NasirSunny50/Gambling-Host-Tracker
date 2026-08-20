"""Storing and retrieving site logins, always through the encryption layer.

Nothing here ever returns a password to a template or a log. The portal can ask whether a
credential exists and when it was set; only the login routine decrypts it, and only in
memory for the moment it types it into the site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ght import crypto
from ght.models import SiteCredential


@dataclass(frozen=True)
class CredentialStatus:
    slug: str
    configured: bool
    label: str | None = None
    updated_at: datetime | None = None


def set_credentials(
    session: Session, slug: str, username: str, password: str, label: str | None = None
) -> None:
    """Encrypt and upsert a site login. Raises if no key is configured (fails closed)."""
    row = session.scalar(select(SiteCredential).where(SiteCredential.slug == slug))
    if row is None:
        row = SiteCredential(slug=slug)
        session.add(row)
    row.username_enc = crypto.encrypt(username)
    row.password_enc = crypto.encrypt(password)
    row.label = label or None
    session.flush()


def get_credentials(session: Session, slug: str) -> tuple[str, str] | None:
    """Decrypt a site login for use. Returns (username, password) or None if unset."""
    row = session.scalar(select(SiteCredential).where(SiteCredential.slug == slug))
    if row is None:
        return None
    return crypto.decrypt(row.username_enc), crypto.decrypt(row.password_enc)


def delete_credentials(session: Session, slug: str) -> bool:
    """Remove a stored login. Returns whether a row was deleted."""
    row = session.scalar(select(SiteCredential).where(SiteCredential.slug == slug))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def status(session: Session, slug: str) -> CredentialStatus:
    """Whether a login exists for a site, and its non-secret label — never the secret."""
    row = session.scalar(select(SiteCredential).where(SiteCredential.slug == slug))
    if row is None:
        return CredentialStatus(slug=slug, configured=False)
    return CredentialStatus(
        slug=slug, configured=True, label=row.label, updated_at=row.updated_at
    )
