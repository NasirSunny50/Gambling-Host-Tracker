"""Portal routes.

Every page that shows account data writes an AccessLog row. That is not decoration: the
point of keeping this data is that someone downstream can be told where a blocklist entry
came from, and who looked at it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, func, literal, select, union_all
from sqlalchemy.orm import Session

from ght.api import TEMPLATES_DIR
from ght.api.jobs import manager
from ght.db import SessionLocal
from ght.models import (
    AccessLog,
    Account,
    AccountSite,
    CollectionRun,
    Evidence,
    MerchantSighting,
    Observation,
    Site,
)
from ght.sources import scan_sources

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

CHANNEL_LABELS = {
    "bkash": "bKash",
    "nagad": "Nagad",
    "rocket": "Rocket",
    "upay": "Upay",
    "tap": "Tap",
    "mcash": "mCash",
    "bank_transfer": "Bank transfer",
}


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _actor(request: Request) -> str:
    """Who is looking. There is no auth in front of this yet, so record what we do know."""
    return request.client.host if request.client else "unknown"


def _log(session: Session, request: Request, action: str, params: dict, rows: int | None) -> None:
    session.add(AccessLog(actor=_actor(request), action=action, params=params, row_count=rows))
    session.commit()


def _age(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - value
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _stamp(value: datetime | None) -> str:
    """An exact timestamp. Relative ages read nicely but hide when something happened,
    which is the thing a case file has to state."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["age"] = _age
templates.env.filters["stamp"] = _stamp
templates.env.filters["channel"] = lambda value: CHANNEL_LABELS.get(value, value)
# The base layout shows a live "collecting…" flag on every page, so the manager is a
# template global rather than something each route has to remember to pass.
templates.env.globals["job"] = manager


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    totals = {
        "accounts": session.scalar(select(func.count()).select_from(Account)) or 0,
        "active": session.scalar(select(func.count()).select_from(Account).where(Account.is_active))
        or 0,
        "review": session.scalar(
            select(func.count()).select_from(Account).where(Account.needs_review)
        )
        or 0,
        "sites": session.scalar(select(func.count()).select_from(Site)) or 0,
    }

    by_channel = session.execute(
        select(Account.channel, func.count())
        .where(Account.is_active)
        .group_by(Account.channel)
        .order_by(func.count().desc())
    ).all()

    runs = session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(8)
    ).all()
    newest = session.scalars(select(Account).order_by(Account.first_seen_at.desc()).limit(8)).all()
    sites = {s.id: s for s in session.scalars(select(Site))}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "totals": totals,
            "by_channel": by_channel,
            "runs": runs,
            "newest": newest,
            "sites": sites,
        },
    )


# Rows per page. Ten by default: a payee list is read row by row, not skimmed, and a short
# page keeps the reader oriented. The other sizes are there for anyone comparing in bulk.
PAGE_SIZE = 10
PAGE_SIZES = (10, 25, 50, 100)


@dataclass(frozen=True)
class Page:
    """One slice of a result set, with what a pager needs to describe it."""

    number: int
    size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, ceil(self.total / self.size))

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def first_row(self) -> int:
        return 0 if not self.total else self.offset + 1

    @property
    def last_row(self) -> int:
        return min(self.offset + self.size, self.total)

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages


def _paginate(
    session: Session, stmt, page_number: int, per_page: int = PAGE_SIZE, scalar: bool = True
) -> tuple[list, Page]:
    """Return one page of rows plus its description.

    The count runs against the same filtered statement, so "of 214" always matches what the
    filters actually select rather than the size of the table. ``scalar`` picks how to read
    the rows: ORM entities come back as objects, a grouped/aggregate select as tuples.
    """
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = Page(number=max(1, page_number), size=per_page, total=total)
    # A stale page number (bookmarked, or the set shrank) should show the last page rather
    # than an empty screen that looks like "nothing matches".
    if page.number > page.pages:
        page = Page(number=page.pages, size=per_page, total=total)
    sliced = stmt.limit(page.size).offset(page.offset)
    rows = session.scalars(sliced).all() if scalar else session.execute(sliced).all()
    return list(rows), page


def _account_query(channel: str | None, status: str, q: str | None):
    stmt = select(Account)
    if channel:
        stmt = stmt.where(Account.channel == channel)
    if status == "active":
        stmt = stmt.where(Account.is_active)
    elif status == "inactive":
        stmt = stmt.where(~Account.is_active)
    elif status == "review":
        stmt = stmt.where(Account.needs_review)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            Account.account_number.like(like)
            | Account.holder_name.like(like)
            | Account.bank_name.like(like)
        )
    return stmt


def _payee_query(channel: str | None, status: str, q: str | None):
    """Accounts and name-only merchants as one list, newest sighting first.

    They are different things — an account is a de-duplicated identity with a number and a
    status, a merchant is a name that changes on every request — but the question a reader
    brings is the same: who is receiving the deposits. A union keeps them in one ordered,
    pageable list without pretending a merchant has fields it does not.
    """
    accounts = select(
        Account.id.label("id"),
        literal("account").label("kind"),
        Account.channel.label("channel"),
        Account.account_number.label("number"),
        Account.holder_name.label("name"),
        Account.bank_name.label("bank"),
        Account.confidence.label("confidence"),
        Account.observation_count.label("times"),
        Account.last_seen_at.label("last_seen"),
        Account.is_active.label("is_active"),
        Account.needs_review.label("needs_review"),
    )
    if channel:
        accounts = accounts.where(Account.channel == channel)
    if status == "active":
        accounts = accounts.where(Account.is_active)
    elif status == "inactive":
        accounts = accounts.where(~Account.is_active)
    elif status == "review":
        accounts = accounts.where(Account.needs_review)
    if q:
        like = f"%{q.strip()}%"
        accounts = accounts.where(
            Account.account_number.like(like)
            | Account.holder_name.like(like)
            | Account.bank_name.like(like)
        )

    merchants = (
        select(
            literal(None).label("id"),
            literal("merchant").label("kind"),
            MerchantSighting.channel.label("channel"),
            literal(None, String).label("number"),
            MerchantSighting.merchant_name.label("name"),
            literal(None, String).label("bank"),
            literal(None).label("confidence"),
            func.count().label("times"),
            func.max(MerchantSighting.seen_at).label("last_seen"),
            literal(None).label("is_active"),
            literal(None).label("needs_review"),
        )
        .group_by(MerchantSighting.merchant_name, MerchantSighting.channel)
    )
    if channel:
        merchants = merchants.where(MerchantSighting.channel == channel)
    if q:
        merchants = merchants.where(MerchantSighting.merchant_name.like(f"%{q.strip()}%"))

    # A merchant has no status to filter on, so any status filter is a filter to accounts.
    if status in {"active", "inactive", "review"}:
        return accounts.order_by(Account.last_seen_at.desc())

    combined = union_all(accounts, merchants).subquery()
    return select(combined).order_by(combined.c.last_seen.desc())


@router.get("/payees", response_class=HTMLResponse)
def payees(
    request: Request,
    channel: str | None = None,
    status: str = "all",
    q: str | None = None,
    page: int = 1,
    per: int = PAGE_SIZE,
    session: Session = Depends(get_session),
):
    """One list of every payee the collector has seen."""
    per_page = per if per in PAGE_SIZES else PAGE_SIZE
    stmt = _payee_query(channel, status, q)
    rows, page_info = _paginate(session, stmt, page, per_page=per_page, scalar=False)
    _log(
        session,
        request,
        "search",
        {"channel": channel, "status": status, "q": q, "page": page_info.number},
        page_info.total,
    )

    channels = sorted(
        set(session.scalars(select(Account.channel).distinct()).all())
        | set(session.scalars(select(MerchantSighting.channel).distinct()).all())
    )
    return templates.TemplateResponse(
        request,
        "payees.html",
        {
            "rows": rows,
            "pagination": page_info,
            "channels": channels,
            "channel": channel,
            "status": status,
            "q": q or "",
            "per": per_page,
            "page_sizes": PAGE_SIZES,
        },
    )


@router.get("/accounts", response_class=HTMLResponse)
def accounts_redirect(request: Request):
    """Kept so existing links and bookmarks survive the merge."""
    query = request.url.query
    return RedirectResponse(f"/payees{'?' + query if query else ''}", status_code=307)


@router.get("/merchants", response_class=HTMLResponse)
def merchants_redirect(request: Request):
    return RedirectResponse("/payees", status_code=307)


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(account_id: int, request: Request, session: Session = Depends(get_session)):
    account = session.get(Account, account_id)
    if account is None:
        return HTMLResponse("<h1>404</h1><p>No such account.</p>", status_code=404)

    observations = session.scalars(
        select(Observation)
        .where(Observation.account_id == account_id)
        .order_by(Observation.observed_at.desc())
        .limit(50)
    ).all()

    sites = session.execute(
        select(Site, AccountSite)
        .join(AccountSite, AccountSite.site_id == Site.id)
        .where(AccountSite.account_id == account_id)
    ).all()

    run_ids = {o.run_id for o in observations}
    evidence = (
        session.scalars(
            select(Evidence).where(Evidence.run_id.in_(run_ids)).order_by(Evidence.id.desc())
        ).all()
        if run_ids
        else []
    )

    _log(session, request, "api_read", {"account_id": account_id}, 1)
    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "account": account,
            "observations": observations,
            "sites": sites,
            "evidence": evidence,
        },
    )


def _runnable_sites() -> list[dict]:
    """Sites the portal offers to collect, with the side effect of each made visible."""
    configs, _ = scan_sources()
    out = []
    for config in configs:
        order_probes = [p.name for p in config.probes if p.creates_order]
        out.append(
            {
                "slug": config.slug,
                "name": config.name,
                "status": config.status,
                "fetcher": config.fetcher,
                "order_probes": order_probes,
            }
        )
    return out


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(100)
    ).all()
    sites = {s.id: s for s in session.scalars(select(Site))}
    counts = dict(
        session.execute(select(Evidence.run_id, func.count()).group_by(Evidence.run_id)).all()
    )
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "rows": rows,
            "sites": sites,
            "evidence_counts": counts,
            "runnable": _runnable_sites(),
            "job_running": manager.is_running,
            "job_log": manager.log_tail,
            # While a run is in flight the page reloads itself so progress is visible
            # without the analyst hammering refresh.
            "auto_refresh": manager.is_running,
        },
    )


@router.post("/runs")
def start_run(request: Request, slug: str = Form(...), session: Session = Depends(get_session)):
    """Kick off a collection in the background. Only known site slugs are accepted."""
    known = {s["slug"] for s in _runnable_sites()}
    if slug not in known:
        return RedirectResponse("/runs", status_code=303)

    started, message = manager.start(slug)
    _log(session, request, "run", {"slug": slug, "started": started, "message": message}, None)
    return RedirectResponse("/runs", status_code=303)


@router.get("/accounts.csv")
def accounts_csv(
    request: Request,
    channel: str | None = None,
    status: str = "all",
    q: str | None = None,
    session: Session = Depends(get_session),
):
    """The same rows the table is showing, as a file the AML team can hand on."""
    rows = session.scalars(_account_query(channel, status, q).order_by(Account.channel)).all()
    _log(session, request, "export", {"channel": channel, "status": status, "q": q}, len(rows))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "channel",
            "account_number",
            "holder_name",
            "bank_name",
            "branch",
            "operator",
            "account_type",
            "confidence",
            "is_active",
            "observations",
            "first_seen_at",
            "last_seen_at",
        ]
    )
    for account in rows:
        writer.writerow(
            [
                account.channel,
                account.account_number,
                account.holder_name or "",
                account.bank_name or "",
                account.branch or "",
                account.operator or "",
                account.account_type or "",
                f"{account.confidence:.2f}",
                "yes" if account.is_active else "no",
                account.observation_count,
                account.first_seen_at.isoformat() if account.first_seen_at else "",
                account.last_seen_at.isoformat() if account.last_seen_at else "",
            ]
        )
    buffer.seek(0)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="accounts-{stamp}.csv"'},
    )

