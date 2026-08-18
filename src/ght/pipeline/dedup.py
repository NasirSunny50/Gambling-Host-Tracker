"""Turning extracted accounts into observations and de-duplicated account rows.

Every sighting appends an ``Observation``. The ``Account`` row behind it is created once
and then only ever has its counters and last-seen timestamp moved forward — the numbers
rotate several times a day, so the history is the product, not a side effect.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ght.models import Account, AccountSite, CollectionRun, Observation, utcnow
from ght.types import CONFIDENCE_HIGH, NormalizedAccount


def _get_or_create_account(
    session: Session, extracted: NormalizedAccount, seen_at: datetime
) -> tuple[Account, bool]:
    channel, number, bank_key = extracted.dedup_key

    account = session.scalar(
        select(Account).where(
            Account.channel == channel,
            Account.account_number == number,
            Account.bank_key == bank_key,
        )
    )
    if account is not None:
        return account, False

    account = Account(
        channel=channel,
        account_number=number,
        bank_key=bank_key,
        account_type=extracted.account_type,
        bank_name=extracted.bank_name,
        branch=extracted.branch,
        holder_name=extracted.holder_name,
        operator=extracted.operator,
        confidence=extracted.confidence,
        needs_review=extracted.confidence < CONFIDENCE_HIGH,
        is_active=True,
        observation_count=0,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )
    session.add(account)
    session.flush()
    return account, True


def _enrich(account: Account, extracted: NormalizedAccount) -> None:
    """Fill in details a later, better-labelled sighting supplied.

    A number first picked up by the regex sweep has no account type and no bank details;
    when a selector hit later confirms the same number, that context is worth keeping.
    """
    if extracted.confidence > account.confidence:
        account.confidence = extracted.confidence
        account.needs_review = extracted.confidence < CONFIDENCE_HIGH
    for field in ("account_type", "bank_name", "branch", "holder_name", "operator"):
        if getattr(account, field) is None and getattr(extracted, field) is not None:
            setattr(account, field, getattr(extracted, field))


def _touch_site_link(session: Session, account: Account, site_id: int, seen_at: datetime) -> None:
    link = session.scalar(
        select(AccountSite).where(
            AccountSite.account_id == account.id, AccountSite.site_id == site_id
        )
    )
    if link is None:
        link = AccountSite(
            account_id=account.id,
            site_id=site_id,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            observation_count=0,
        )
        session.add(link)
    link.last_seen_at = seen_at
    link.observation_count += 1


def record_observations(
    session: Session,
    run: CollectionRun,
    site_id: int,
    extracted_accounts: list[NormalizedAccount],
    page_url: str | None = None,
) -> tuple[list[int], list[int]]:
    """Persist one run's findings.

    Returns ``(account_ids_seen, account_ids_new)``.
    """
    seen_at = run.started_at or utcnow()
    seen_ids: list[int] = []
    new_ids: list[int] = []

    for extracted in extracted_accounts:
        account, is_new = _get_or_create_account(session, extracted, seen_at)
        if is_new:
            new_ids.append(account.id)
        else:
            _enrich(account, extracted)

        account.last_seen_at = seen_at
        account.observation_count += 1
        account.is_active = True

        session.add(
            Observation(
                run_id=run.id,
                site_id=site_id,
                account_id=account.id,
                raw_text=extracted.raw_text,
                page_url=page_url,
                origin=extracted.origin,
                confidence=extracted.confidence,
                observed_at=seen_at,
            )
        )
        _touch_site_link(session, account, site_id, seen_at)
        seen_ids.append(account.id)

    session.flush()
    return seen_ids, new_ids
