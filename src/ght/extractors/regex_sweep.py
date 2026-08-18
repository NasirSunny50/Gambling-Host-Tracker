"""Full-page regex sweep: the high-recall path.

This ignores the configured selectors entirely and scans the rendered text for anything
shaped like a BD payment account. On its own it is noisy, so its hits are recorded at low
confidence. Its real job is to be a witness: when the selectors return nothing but the
sweep still finds numbers, the site has been redesigned and the config is stale.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from ght.normalize.bank import find_bank_accounts
from ght.normalize.channel import CHANNEL_BANK, MFS_CHANNELS, channel_near
from ght.normalize.msisdn import MSISDN_PATTERN, translate_digits
from ght.types import Candidate

REGEX_SWEEP_ORIGIN = "regex_sweep"
CONTEXT_RADIUS = 200


def page_text(html: str) -> str:
    """Visible text of the page, with script and style content dropped."""
    if not html:
        return ""
    tree = HTMLParser(html)
    tree.strip_tags(["script", "style", "noscript"])
    node = tree.body or tree.root
    if node is None:
        return ""
    return " ".join(node.text(separator=" ", strip=True).split())


def _context_around(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_RADIUS) : end + CONTEXT_RADIUS]


def sweep(html: str) -> list[Candidate]:
    """Find every payment-account-shaped string on the page, with its surrounding text."""
    text = translate_digits(page_text(html))
    if not text:
        return []

    candidates: list[Candidate] = []
    seen: set[str] = set()

    for match in MSISDN_PATTERN.finditer(text):
        raw = match.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        candidates.append(
            Candidate(
                raw_text=raw,
                context=_context_around(text, match.start(), match.end()),
                position=match.start(),
                origin=REGEX_SWEEP_ORIGIN,
                # Nothing in config vouches for this one, so the channel is whatever MFS
                # brand is written closest to it. Bank transfer is excluded on purpose: a
                # mobile number is never a bank account number.
                channel_hint=channel_near(text, match.start(), only=MFS_CHANNELS),
            )
        )

    for hit in find_bank_accounts(text):
        if hit.account_number in seen:
            continue
        seen.add(hit.account_number)
        end = hit.position + len(hit.account_number)
        candidates.append(
            Candidate(
                raw_text=hit.account_number,
                context=_context_around(text, hit.position, end),
                position=hit.position,
                origin=REGEX_SWEEP_ORIGIN,
                channel_hint=CHANNEL_BANK,
            )
        )

    return candidates
