"""Fetcher interface.

A fetcher's only job is to return exactly what the server sent, untouched. Parsing,
cleaning and extraction all happen later, because the bytes captured here are what gets
hashed and kept as evidence.
"""

from __future__ import annotations

from typing import Protocol

from ght.types import RawCapture

# Signs that a response is a bot wall rather than the page we asked for. These are worth
# separating from ordinary failures: a blocked run needs a different fix from a dead site.
CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "just a moment",
    "checking your browser",
    "attention required",
    "captcha-delivery",
    "access denied",
)


class Fetcher(Protocol):
    name: str

    def fetch(self, url: str) -> RawCapture: ...


def looks_blocked(capture: RawCapture) -> bool:
    """True when the response is a challenge or bot wall rather than real content."""
    if capture.status_code in (403, 429, 503):
        return True
    body = (capture.html or "")[:4000].lower()
    return any(marker in body for marker in CHALLENGE_MARKERS)
