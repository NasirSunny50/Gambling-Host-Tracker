"""Dataclasses passed between the fetch → extract → normalize → persist stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Confidence tiers. Anything below HIGH is held back from an automatic blocklist feed and
# routed to the manual review queue instead.
CONFIDENCE_HIGH = 0.9  # matched a configured selector that names the channel
CONFIDENCE_MEDIUM = 0.6  # matched a selector, channel inferred from nearby text
CONFIDENCE_LOW = 0.3  # found only by the full-page regex sweep


@dataclass(frozen=True)
class RawCapture:
    """Everything a fetcher retrieved for one URL, before any parsing.

    The raw bytes are what gets hashed and stored as evidence, so this must stay an
    untouched copy of what the server sent.
    """

    url: str
    status_code: int
    html: str | None = None
    json_body: str | None = None
    screenshot: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    fetcher: str = "http"
    fetched_at: datetime | None = None
    error: str | None = None
    # Set when a configured click flow broke partway. Kept apart from ``error`` on
    # purpose: the fetch itself succeeded, so the page we did reach is still worth storing
    # as evidence and still worth extracting from. Only the navigation went wrong.
    flow_error: str | None = None
    # Set when the site answered the flow with its own "this method is unavailable" panel.
    # Deliberately not a flow_error: nothing on our side is broken and there is nothing to
    # repair, so a run carrying only these is still a healthy run - it just saw less.
    unavailable: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        """The body to run extraction against, whichever form the site returned."""
        return self.html or self.json_body or ""


@dataclass(frozen=True)
class Candidate:
    """A raw hit from an extractor, before validation or normalization."""

    raw_text: str
    context: str
    position: int
    origin: str  # selector that produced it, or "regex_sweep"
    channel_hint: str | None = None
    # Payee name lifted from a configured `holder` selector. Unlike bank pages, an MFS
    # block usually prints the name as a bare element with no "A/C Name:" label, so there
    # is nothing for the bank normalizer's regex to find — config has to point at it.
    holder_hint: str | None = None
    # Bank named by config rather than read off the page.
    bank_hint: str | None = None


@dataclass(frozen=True)
class NormalizedAccount:
    """A validated payment account ready to be stored as an observation."""

    channel: str
    account_number: str
    raw_text: str
    confidence: float
    origin: str
    account_type: str | None = None
    bank_name: str | None = None
    branch: str | None = None
    holder_name: str | None = None
    operator: str | None = None

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        """Identity of the underlying account, matching the DB unique index."""
        return (self.channel, self.account_number, self.bank_name or "")
