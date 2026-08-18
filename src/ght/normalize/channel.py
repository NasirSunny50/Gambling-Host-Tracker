"""Payment channel classification.

A number alone never tells you the channel — a bKash wallet and a Nagad wallet can both
live on the same 017 number. The channel has to come from the surrounding page context:
the label beside the field, the section heading, an icon's alt text, a CSS class.
"""

from __future__ import annotations

import re

CHANNEL_BKASH = "bkash"
CHANNEL_NAGAD = "nagad"
CHANNEL_ROCKET = "rocket"
CHANNEL_UPAY = "upay"
CHANNEL_TAP = "tap"
CHANNEL_MCASH = "mcash"
CHANNEL_BANK = "bank_transfer"

# Ordered most-specific first: a block mentioning both "bKash" and "bank" is a bKash block.
# Each entry is (channel, regex). Word boundaries matter — "tap" and "upay" are short
# enough to appear inside unrelated words.
_CHANNEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (CHANNEL_BKASH, re.compile(r"\bb[\s\-]?kash\b|বিকাশ", re.IGNORECASE)),
    (CHANNEL_NAGAD, re.compile(r"\bnagad\b|নগদ", re.IGNORECASE)),
    (CHANNEL_ROCKET, re.compile(r"\brocket\b|রকেট|\bdbbl\s+mobile\b", re.IGNORECASE)),
    (CHANNEL_UPAY, re.compile(r"\bupay\b|উপায়", re.IGNORECASE)),
    (CHANNEL_TAP, re.compile(r"\btap\b(?!\s+(?:here|to|on))|ট্যাপ", re.IGNORECASE)),
    (CHANNEL_MCASH, re.compile(r"\bm[\s\-]?cash\b", re.IGNORECASE)),
    (CHANNEL_BANK, re.compile(r"\bbank\b|ব্যাংক|\ba/?c\s*(?:no|number)|\brouting\b", re.IGNORECASE)),
]

MFS_CHANNELS = frozenset(
    {CHANNEL_BKASH, CHANNEL_NAGAD, CHANNEL_ROCKET, CHANNEL_UPAY, CHANNEL_TAP, CHANNEL_MCASH}
)
ALL_CHANNELS = MFS_CHANNELS | {CHANNEL_BANK}

_ACCOUNT_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("agent", re.compile(r"\bagent\b|এজেন্ট", re.IGNORECASE)),
    ("merchant", re.compile(r"\bmerchant\b|মার্চেন্ট", re.IGNORECASE)),
    ("personal", re.compile(r"\bpersonal\b|\bsend\s+money\b|পার্সোনাল", re.IGNORECASE)),
]


def classify_channel(context: str) -> str | None:
    """Return the payment channel named in ``context``, most specific brand winning."""
    if not context:
        return None
    for channel, pattern in _CHANNEL_PATTERNS:
        if pattern.search(context):
            return channel
    return None


def channel_near(
    text: str, position: int, window: int = 400, only: frozenset[str] | None = None
) -> str | None:
    """Return the channel whose brand mention sits closest to ``position`` in ``text``.

    Used by the regex sweep, where one page carries several channel blocks and a match's
    channel is decided by proximity rather than by document order. Only mentions within
    ``window`` characters count, so an unrelated brand far up the page cannot claim a
    number that has no label of its own.

    Pass ``only`` to restrict the search to a subset of channels — a mobile number is never
    a bank account, so looking it up against ``MFS_CHANNELS`` stops a nearby "Bank Transfer"
    heading from stealing it.
    """
    if not text:
        return None

    best_channel: str | None = None
    best_distance = window + 1
    for channel, pattern in _CHANNEL_PATTERNS:
        if only is not None and channel not in only:
            continue
        for match in pattern.finditer(text):
            # Distance from the number to the nearest edge of the brand mention.
            if match.end() <= position:
                distance = position - match.end()
            elif match.start() >= position:
                distance = match.start() - position
            else:
                distance = 0
            if distance < best_distance:
                best_distance = distance
                best_channel = channel
    return best_channel


def classify_account_type(context: str) -> str | None:
    """Return personal / agent / merchant when the context says so, else ``None``."""
    if not context:
        return None
    for account_type, pattern in _ACCOUNT_TYPE_PATTERNS:
        if pattern.search(context):
            return account_type
    return None
