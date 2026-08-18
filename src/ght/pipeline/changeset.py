"""Working out what changed between one run and the last.

Two different questions, deliberately answered separately:

* *What stopped being advertised?* — compared against this site's previous successful run,
  so the AML team hears about it on the next collection rather than a day later.
* *What should still be blocklisted?* — a number that rotated out this morning is still
  worth blocking this afternoon, so ``is_active`` decays over a window instead of flipping
  off the moment a number leaves the page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ght.models import Account, AccountSite, CollectionRun, Observation, utcnow

# How long an account stays flagged active after its last sighting anywhere.
ACTIVE_WINDOW = timedelta(hours=48)


@dataclass
class ChangeSet:
    new_account_ids: list[int] = field(default_factory=list)
    reappeared_account_ids: list[int] = field(default_factory=list)
    disappeared_account_ids: list[int] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_account_ids or self.reappeared_account_ids or self.disappeared_account_ids
        )


def _previous_run_account_ids(session: Session, site_id: int, before_run_id: int) -> set[int]:
    """Account ids seen in the last successful run of this site before ``before_run_id``."""
    previous = session.scalar(
        select(CollectionRun)
        .where(
            CollectionRun.site_id == site_id,
            CollectionRun.id < before_run_id,
            CollectionRun.status.in_(("ok", "partial")),
        )
        .order_by(CollectionRun.id.desc())
        .limit(1)
    )
    if previous is None:
        return set()
    rows = session.scalars(
        select(Observation.account_id).where(Observation.run_id == previous.id)
    ).all()
    return set(rows)


def compute_changeset(
    session: Session,
    run: CollectionRun,
    site_id: int,
    seen_ids: list[int],
    new_ids: list[int],
    complete: bool = True,
) -> ChangeSet:
    """Compare this run against the site's previous successful run.

    ``complete`` says whether this run actually saw everything it was meant to. When a
    probe fails — an expired login, a moved button — the run comes back with a partial
    view of the page, and every account it could not reach looks identical to one that has
    been taken down. Concluding "gone" from a partial view produces false retirements and
    the alerts that go with them, so absence is only inferred from a full sweep.
    """
    seen = set(seen_ids)
    previous = _previous_run_account_ids(session, site_id, run.id)

    changes = ChangeSet(new_account_ids=list(new_ids))
    # Seen before, absent last time, back now — a rotating pool cycling round again.
    changes.reappeared_account_ids = sorted(seen - previous - set(new_ids))
    changes.disappeared_account_ids = sorted(previous - seen) if complete else []
    return changes


def refresh_active_flags(session: Session, account_ids: list[int] | None = None) -> int:
    """Recompute ``is_active`` from the last sighting, and return how many rows changed.

    Scoped to ``account_ids`` when given, so a single site's run does not have to walk the
    whole table.
    """
    cutoff = utcnow() - ACTIVE_WINDOW

    query = select(Account)
    if account_ids:
        query = query.where(Account.id.in_(account_ids))

    changed = 0
    for account in session.scalars(query):
        latest = session.scalar(
            select(AccountSite.last_seen_at)
            .where(AccountSite.account_id == account.id)
            .order_by(AccountSite.last_seen_at.desc())
            .limit(1)
        )
        should_be_active = latest is not None and _as_aware(latest) >= cutoff
        if account.is_active != should_be_active:
            account.is_active = should_be_active
            changed += 1

    session.flush()
    return changed


def _as_aware(value):
    """SQLite hands back naive datetimes; treat those as UTC."""
    if value.tzinfo is None:
        from datetime import UTC

        return value.replace(tzinfo=UTC)
    return value
