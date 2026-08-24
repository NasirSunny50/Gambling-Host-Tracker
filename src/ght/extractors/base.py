"""Combining the two extraction paths and turning their hits into accounts."""

from __future__ import annotations

from dataclasses import dataclass, field

from ght.extractors.css import extract_with_selectors
from ght.extractors.regex_sweep import REGEX_SWEEP_ORIGIN, sweep
from ght.normalize.bank import find_bank_accounts, holder_name_in
from ght.normalize.channel import (
    CHANNEL_BANK,
    CHANNEL_ROCKET,
    classify_account_type,
    classify_channel,
)
from ght.normalize.msisdn import find_msisdns, normalize_msisdn, normalize_rocket, operator_of
from ght.sources import SourceConfig
from ght.types import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    Candidate,
    NormalizedAccount,
)


@dataclass
class ExtractionResult:
    accounts: list[NormalizedAccount] = field(default_factory=list)
    selector_hits: int = 0
    sweep_hits: int = 0

    @property
    def extractor_looks_broken(self) -> bool:
        """True when the sweep found numbers the configured selectors did not.

        This is the signal that a site redesign has silently orphaned the config. Without
        it a stale selector just returns zero numbers every run and looks like a quiet day.
        """
        return self.selector_hits == 0 and self.sweep_hits > 0

    @property
    def review_count(self) -> int:
        return sum(1 for account in self.accounts if account.confidence < CONFIDENCE_HIGH)


def _clean_holder_hint(candidate: Candidate) -> str | None:
    """The payee's name, refusing anything that is really the number again.

    These panels reuse one element class for every value they print, so the configured
    holder selector lands on the number itself whenever a method lists the number and not
    the name. Storing that would put a phone number in the field an investigator reads as
    an identity.
    """
    holder = candidate.holder_hint
    if holder:
        digits = "".join(ch for ch in holder if ch.isdigit())
        if digits and len(digits) >= len(holder.replace(" ", "")) - 2:
            holder = None
    return holder or holder_name_in(candidate.context)


def _normalize_candidate(candidate: Candidate, ignored: set[str]) -> NormalizedAccount | None:
    """Turn one raw hit into a validated account, or drop it."""
    from_selector = candidate.origin != REGEX_SWEEP_ORIGIN

    # A mobile number is the common case; try that first.
    msisdns = find_msisdns(candidate.raw_text)
    channel_here = candidate.channel_hint or classify_channel(candidate.context)
    if not msisdns and channel_here == CHANNEL_ROCKET:
        # Rocket alone publishes twelve digits - the wallet's mobile number plus a check
        # digit - which the MSISDN pattern refuses on purpose, since biting eleven digits
        # out of a longer run is how a bank account becomes a phone number. Only a block
        # that says it is Rocket may read it that way, and it keeps all twelve.
        rocket = normalize_rocket(candidate.raw_text)
        if rocket:
            msisdns = [rocket]
    if msisdns:
        number = msisdns[0]
        if number in ignored:
            return None
        channel = channel_here
        if channel is None or channel == CHANNEL_BANK:
            # An MFS wallet with no brand near it cannot be attributed, and a mobile
            # number sitting inside a bank-transfer block is a helpline, not a wallet.
            # Storing either under a guessed channel is worse than not storing it.
            return None
        if from_selector:
            confidence = CONFIDENCE_HIGH if candidate.channel_hint else CONFIDENCE_MEDIUM
        else:
            confidence = CONFIDENCE_LOW
        return NormalizedAccount(
            channel=channel,
            account_number=number,
            raw_text=candidate.raw_text,
            confidence=confidence,
            origin=candidate.origin,
            account_type=classify_account_type(candidate.context),
            # These panels label the payee "wallet name" beside the number rather than
            # giving it an element of its own, so the configured selector often has
            # nothing to point at and the label in the surrounding text is all there is.
            holder_name=_clean_holder_hint(candidate),
            operator=operator_of(number),
        )

    # A block that names its bank vouches for the digits itself: the page shows the number
    # under a plain "account number" label and puts the bank in a dropdown, so requiring a
    # bank name in the surrounding text would throw the account away.
    if candidate.bank_hint:
        digits = "".join(ch for ch in candidate.raw_text if ch.isdigit())
        # Knowing the bank does not make every digit run an account: the same panel prints
        # a row of preset deposit amounts, which flattens into a run of exactly the right
        # length. Everything except the bank lookup still has to pass.
        hits = find_bank_accounts(candidate.raw_text, require_bank=False)
        if not any(hit.account_number == digits for hit in hits) or digits in ignored:
            return None
        return NormalizedAccount(
            channel=CHANNEL_BANK,
            account_number=digits,
            raw_text=candidate.raw_text,
            confidence=CONFIDENCE_HIGH if from_selector else CONFIDENCE_LOW,
            origin=candidate.origin,
            bank_name=candidate.bank_hint,
            holder_name=_clean_holder_hint(candidate),
        )

    # Otherwise it may be a bank account, which only counts with a bank name in context.
    bank_hits = find_bank_accounts(candidate.context) or find_bank_accounts(
        f"{candidate.context} {candidate.raw_text}"
    )
    digits = "".join(ch for ch in candidate.raw_text if ch.isdigit())
    for hit in bank_hits:
        if hit.account_number != digits:
            continue
        if hit.account_number in ignored:
            return None
        return NormalizedAccount(
            channel=CHANNEL_BANK,
            account_number=hit.account_number,
            raw_text=candidate.raw_text,
            confidence=CONFIDENCE_HIGH if from_selector else CONFIDENCE_LOW,
            origin=candidate.origin,
            bank_name=hit.bank_name,
            branch=hit.branch,
            # A labelled "A/C Name" in the block beats a configured selector, which on a
            # shared container can pick up the heading of a neighbouring account.
            holder_name=hit.holder_name or candidate.holder_hint,
        )
    return None


def extract(html: str, config: SourceConfig) -> ExtractionResult:
    """Run both extraction paths and merge them into one set of accounts.

    Selector hits win: when both paths find the same account, the selector's higher
    confidence and richer context are what gets kept.
    """
    ignored = {normalize_msisdn(n) or n for n in config.ignore_numbers}

    selector_candidates = extract_with_selectors(html, config)
    sweep_candidates = sweep(html)

    result = ExtractionResult()
    by_identity: dict[tuple[str, str, str], NormalizedAccount] = {}

    for candidate in selector_candidates:
        account = _normalize_candidate(candidate, ignored)
        if account is None:
            continue
        result.selector_hits += 1
        by_identity.setdefault(account.dedup_key, account)

    # The sweep decides a number's channel by whichever brand name sits closest to it in
    # the flattened page text, which is often the *next* block's heading. Any number a
    # selector already claimed is therefore off limits — otherwise one bKash wallet also
    # gets stored as a phantom Nagad account.
    claimed_numbers = {account.account_number for account in by_identity.values()}

    for candidate in sweep_candidates:
        account = _normalize_candidate(candidate, ignored)
        if account is None or account.account_number in claimed_numbers:
            continue
        result.sweep_hits += 1
        by_identity.setdefault(account.dedup_key, account)

    result.accounts = list(by_identity.values())
    return result
