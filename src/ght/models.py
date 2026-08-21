"""Database schema.

Two ideas drive this design:

* ``Observation`` rows are append-only. Deposit numbers rotate two or three times a day,
  so overwriting a row would destroy exactly the history an AML case needs.
* ``Account`` rows are the de-duplicated entity behind those observations, carrying
  first/last seen and whether the account is still in use.

Column types are kept portable (JSON, not JSONB; no ARRAY) so the same models run on
SQLite locally and PostgreSQL in production.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Site(Base):
    """A target site. Extraction rules live in sources/<slug>.yaml, not here."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | paused | dead
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    urls: Mapped[list[SiteUrl]] = relationship(back_populates="site", cascade="all, delete-orphan")
    runs: Mapped[list[CollectionRun]] = relationship(back_populates="site")


class SiteUrl(Base):
    """Known URLs for a site, including mirrors.

    These operators rotate domains constantly, so a site keeps a list and the collector
    falls back through it when the current one stops resolving.
    """

    __tablename__ = "site_urls"
    __table_args__ = (UniqueConstraint("site_id", "url", name="uq_site_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    url_type: Mapped[str] = mapped_column(String(32), default="deposit")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship(back_populates="urls")


class CollectionRun(Base):
    """One fetch attempt against one site."""

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    url: Mapped[str | None] = mapped_column(String(500))
    fetcher: Mapped[str] = mapped_column(String(32), default="http")
    # ok | partial | failed | blocked - "blocked" (403 / challenge page) is tracked
    # separately from "failed" because it calls for a different fix.
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # Payees this run brought back: de-duplicated accounts plus name-only merchants.
    # Rows written before 2026-08-22 hold raw extraction hits and exclude merchants.
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    accounts_new: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship(back_populates="runs")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="run")
    observations: Mapped[list[Observation]] = relationship(back_populates="run")


class Evidence(Base):
    """A stored copy of what the server actually returned.

    The sha256 is what makes an observation defensible later: the blob on disk can be
    re-hashed and shown to be the same bytes that produced the extracted number.
    """

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # html | json | screenshot
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    bytes: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[CollectionRun] = relationship(back_populates="evidence")


class Account(Base):
    """A unique payment account, de-duplicated across sites and across runs."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("channel", "account_number", "bank_key", name="uq_account_identity"),
        Index("ix_accounts_number", "account_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(24), index=True)
    account_number: Mapped[str] = mapped_column(String(32))
    # Empty string rather than NULL for MFS wallets, because NULL never compares equal in
    # a unique constraint and the same wallet would be inserted again on every run.
    bank_key: Mapped[str] = mapped_column(String(100), default="")

    account_type: Mapped[str | None] = mapped_column(String(16))
    bank_name: Mapped[str | None] = mapped_column(String(100))
    branch: Mapped[str | None] = mapped_column(String(100))
    holder_name: Mapped[str | None] = mapped_column(String(120))
    operator: Mapped[str | None] = mapped_column(String(32))

    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    observations: Mapped[list[Observation]] = relationship(back_populates="account")
    site_links: Mapped[list[AccountSite]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Observation(Base):
    """One sighting of one account on one site during one run. Never updated."""

    __tablename__ = "observations"
    __table_args__ = (Index("ix_observations_account_time", "account_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    page_url: Mapped[str | None] = mapped_column(String(500))
    origin: Mapped[str] = mapped_column(String(120))  # selector, or "regex_sweep"
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    run: Mapped[CollectionRun] = relationship(back_populates="observations")
    account: Mapped[Account] = relationship(back_populates="observations")


class AccountSite(Base):
    """Link between an account and a site.

    One account appearing on several sites is the strongest signal this schema produces:
    it means the same payment operator is collecting for multiple brands.
    """

    __tablename__ = "account_sites"
    __table_args__ = (UniqueConstraint("account_id", "site_id", name="uq_account_site"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)

    account: Mapped[Account] = relationship(back_populates="site_links")


class ExcludedNumber(Base):
    """Numbers published on these pages that are not deposit accounts.

    Support hotlines and WhatsApp contacts sit right beside the deposit numbers and would
    otherwise be collected and blocklisted alongside them.
    """

    __tablename__ = "excluded_numbers"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # new_account | account_disappeared | extractor_broken | flow_broken
    # | probe_failed | site_down | site_blocked
    type: Mapped[str] = mapped_column(String(32), index=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id"))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchantSighting(Base):
    """A payee that is identified by name only, with no account number to key on.

    Some aggregators never show a receiving wallet: the deposit hands off to the payment
    provider's own checkout, which names a merchant and an invoice and asks the payer for
    their own number. That merchant is real intelligence - it rotates per request, so the
    names accumulate into a picture of the pool an operator is drawing on - but it cannot
    be stored as an Account, which is keyed on a number that does not exist here.

    So these are kept as append-only sightings rather than deduplicated identities. Nothing
    "disappears" from this table, because a name not shown today says nothing about whether
    it is still in use.
    """

    __tablename__ = "merchant_sightings"
    __table_args__ = (Index("ix_merchant_name_time", "merchant_name", "seen_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("collection_runs.id"), index=True)
    # Which configured probe produced it, so a name can be traced back to a method.
    probe: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(24), index=True)
    merchant_name: Mapped[str] = mapped_column(String(200), index=True)
    page_url: Mapped[str | None] = mapped_column(String(500))
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AccessLog(Base):
    """Who queried or exported the account data. Required for AML data governance."""

    __tablename__ = "access_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(64))  # search | export | api_read
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    row_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

