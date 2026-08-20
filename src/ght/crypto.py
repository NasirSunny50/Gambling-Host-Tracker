"""Encryption for credentials at rest.

Site logins are stored encrypted, never in plaintext, because this database is deployed
inside a bank. A symmetric key (Fernet) is read from the ``GHT_SECRET_KEY`` environment
variable and never written to disk by this code.

The design fails closed: with no key configured, credentials cannot be stored or read at
all, rather than silently falling back to plaintext. A misconfigured deployment loses the
feature; it never leaks the secret.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ght.config import settings


class SecretKeyMissing(RuntimeError):
    """Raised when a credential operation is attempted without GHT_SECRET_KEY set."""


class SecretKeyInvalid(RuntimeError):
    """Raised when the configured key is not a valid Fernet key."""


def generate_key() -> str:
    """A fresh urlsafe-base64 key to put in GHT_SECRET_KEY. Not stored by this process."""
    return Fernet.generate_key().decode("ascii")


def is_configured() -> bool:
    """True when a usable key is present, so the UI can guide setup before it is needed."""
    if not settings.secret_key:
        return False
    try:
        _fernet()
        return True
    except SecretKeyInvalid:
        return False


def _fernet() -> Fernet:
    if not settings.secret_key:
        raise SecretKeyMissing(
            "GHT_SECRET_KEY is not set. Generate one with `ght gen-key` and put it in the "
            "environment before storing credentials."
        )
    try:
        return Fernet(settings.secret_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise SecretKeyInvalid(f"GHT_SECRET_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. The token is safe to keep in the database."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Recover a secret. Raises SecretKeyInvalid if the key cannot read this token."""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # Wrong key, or data written under a rotated key. Never echo the token.
        raise SecretKeyInvalid(
            "stored credential could not be decrypted with the current GHT_SECRET_KEY"
        ) from exc
