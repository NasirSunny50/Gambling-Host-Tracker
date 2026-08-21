"""Portal routes.

Every page that shows account data writes an AccessLog row. That is not decoration: the
point of keeping this data is that someone downstream can be told where a blocklist entry
came from, and who looked at it.
"""

from __future__ import annotations

import csv
import io
import re
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


# How the newest run's outcome reads in the header. There is no alerts page: whether
# collection is healthy has to be legible from the chrome of whatever page you are on.
_HEALTH = {
    "ok": ("collection healthy", "ok"),
    "partial": ("collection degraded", "warn"),
    "failed": ("last run failed", "bad"),
    "blocked": ("site is blocking us", "bad"),
}


def _nav(session: Session) -> dict:
    """The two things the shell shows on every page: what needs a human, and whether
    collection is working. Both are counts, so this costs a page load almost nothing."""
    review = session.scalar(select(func.count()).select_from(Account).where(Account.needs_review))
    latest = session.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
    health, tone = _HEALTH.get(latest.status, (latest.status, "warn")) if latest else ("no runs yet", "idle")
    return {
        "review": review or 0,
        "health": health,
        "health_tone": tone,
        # Local time, to match every timestamp shown in the tables below it.
        "now": datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M"),
    }


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
            "nav": _nav(session),
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
            "nav": _nav(session),
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


# How many saved pages the detail view lists before it stops and gives the total instead.
EVIDENCE_SHOWN = 20


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

    # Every page saved by every run that saw this account. A long-lived number can have
    # hundreds, so the page shows the most recent and states the true total rather than
    # rendering the lot.
    run_ids = {o.run_id for o in observations}
    evidence_total = 0
    evidence = []
    if run_ids:
        evidence_total = (
            session.scalar(
                select(func.count()).select_from(Evidence).where(Evidence.run_id.in_(run_ids))
            )
            or 0
        )
        evidence = session.scalars(
            select(Evidence)
            .where(Evidence.run_id.in_(run_ids))
            .order_by(Evidence.captured_at.desc())
            .limit(EVIDENCE_SHOWN)
        ).all()

    _log(session, request, "api_read", {"account_id": account_id}, 1)
    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "nav": _nav(session),
            "account": account,
            "observations": observations,
            "sites": sites,
            "evidence": evidence,
            "evidence_total": evidence_total,
            "evidence_shown": EVIDENCE_SHOWN,
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


def _is_waiting(info) -> bool:
    """Whether the run is paused for the operator rather than working.

    This is the one state the page must never let read as "busy" or as "broken": the run
    is fine, it is standing still until a person signs in to a window that opened
    elsewhere. The progress messages that mean it are the ones auth_login emits.
    """
    message = (info.message or "").lower()
    return "waiting for you" in message or "opening a browser window" in message


def _seconds_left(info) -> int | None:
    """The sign-in countdown, recovered from the progress message that carries it."""
    match = re.search(r"(\d+)s left", info.message or "")
    return int(match.group(1)) if match else None


def _elapsed(info) -> str:
    """How long the run has been going, or took. Wall-clock, in the form an operator
    would say it out loud."""
    end = info.finished_at or datetime.now(UTC)
    start = info.started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    seconds = max(0, int((end - start).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, session: Session = Depends(get_session)):
    rows = session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(100)
    ).all()
    sites = {s.id: s for s in session.scalars(select(Site))}
    counts = dict(
        session.execute(select(Evidence.run_id, func.count()).group_by(Evidence.run_id)).all()
    )

    info = manager.current
    waiting = bool(info) and manager.is_running and _is_waiting(info)
    phases = info.phases if info else []
    # A phase the operator is blocking is not "in progress" — it is waiting on them, and
    # the checklist has to say so rather than spinning as though work were happening.
    if waiting:
        phases = [{**p, "state": "waiting" if p["state"] == "active" else p["state"]} for p in phases]

    # The run row the finished summary describes: the newest one for that site.
    last_run = None
    if info and not manager.is_running:
        last_run = next(
            (r for r in rows if r.site_id in sites and sites[r.site_id].slug == info.slug), None
        )

    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "nav": _nav(session),
            "rows": rows,
            "sites": sites,
            "evidence_counts": counts,
            "runnable": _runnable_sites(),
            "job_running": manager.is_running,
            "job_log": manager.log_tail,
            "waiting": waiting,
            "seconds_left": _seconds_left(info) if waiting else None,
            "phases": phases,
            "elapsed": _elapsed(info) if info else "",
            "last_run": last_run,
            # While a run is in flight the page reloads itself so progress is visible
            # without the analyst hammering refresh.
            "auto_refresh": manager.is_running,
        },
    )


@router.get("/components", response_class=HTMLResponse)
def components(request: Request, session: Session = Depends(get_session)):
    """The recurring elements and why they read the way they do.

    It is in the portal rather than in a document because the elements drift: a label that
    changed in a template is wrong in a screenshot the next day, but right here.
    """
    return templates.TemplateResponse(request, "components.html", {"nav": _nav(session)})


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

