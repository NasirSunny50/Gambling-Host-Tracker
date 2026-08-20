"""Portal routes.

Every page that shows account data writes an AccessLog row. That is not decoration: the
point of keeping this data is that someone downstream can be told where a blocklist entry
came from, and who looked at it.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ght import credentials as creds
from ght import crypto
from ght.api import TEMPLATES_DIR
from ght.api.jobs import login_manager, manager
from ght.config import REPO_ROOT as REPO_ROOT_FALLBACK
from ght.db import SessionLocal
from ght.models import (
    AccessLog,
    Account,
    AccountSite,
    Alert,
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


templates.env.filters["age"] = _age
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
    alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(10)).all()
    newest = session.scalars(select(Account).order_by(Account.first_seen_at.desc()).limit(8)).all()
    sites = {s.id: s for s in session.scalars(select(Site))}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "totals": totals,
            "by_channel": by_channel,
            "runs": runs,
            "alerts": alerts,
            "newest": newest,
            "sites": sites,
        },
    )


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


@router.get("/accounts", response_class=HTMLResponse)
def accounts(
    request: Request,
    channel: str | None = None,
    status: str = "all",
    q: str | None = None,
    session: Session = Depends(get_session),
):
    rows = session.scalars(
        _account_query(channel, status, q).order_by(Account.last_seen_at.desc())
    ).all()
    _log(session, request, "search", {"channel": channel, "status": status, "q": q}, len(rows))

    channels = session.scalars(select(Account.channel).distinct().order_by(Account.channel)).all()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "rows": rows,
            "channels": channels,
            "channel": channel,
            "status": status,
            "q": q or "",
        },
    )


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


@router.get("/alerts", response_class=HTMLResponse)
def alerts(request: Request, session: Session = Depends(get_session)):
    rows = session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(200)).all()
    sites = {s.id: s for s in session.scalars(select(Site))}
    accounts_by_id = {a.id: a for a in session.scalars(select(Account))}
    return templates.TemplateResponse(
        request, "alerts.html", {"rows": rows, "sites": sites, "accounts": accounts_by_id}
    )


@router.get("/merchants", response_class=HTMLResponse)
def merchants(request: Request, session: Session = Depends(get_session)):
    """Payees that only ever showed a name.

    Grouped by name rather than listed raw: the interesting quantity is how many distinct
    merchants an operator cycles through, and how recently each was used.
    """
    grouped = session.execute(
        select(
            MerchantSighting.merchant_name,
            MerchantSighting.channel,
            MerchantSighting.probe,
            func.count().label("times"),
            func.min(MerchantSighting.seen_at).label("first_seen"),
            func.max(MerchantSighting.seen_at).label("last_seen"),
        )
        .group_by(
            MerchantSighting.merchant_name, MerchantSighting.channel, MerchantSighting.probe
        )
        .order_by(func.max(MerchantSighting.seen_at).desc())
    ).all()
    total = session.scalar(select(func.count()).select_from(MerchantSighting)) or 0
    _log(session, request, "search", {"view": "merchants"}, len(grouped))
    return templates.TemplateResponse(
        request, "merchants.html", {"rows": grouped, "total": total}
    )


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


def _session_age_hours(auth_state: str | None) -> float | None:
    """Age of a saved session file in hours, or None if the site has none yet."""
    if not auth_state:
        return None
    path = Path(auth_state)
    if not path.is_absolute():
        path = REPO_ROOT_FALLBACK / path
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return (datetime.now(UTC) - mtime).total_seconds() / 3600


def _site_rows(session: Session) -> list[dict]:
    """Per-site view for the credentials page: what is stored, and how sign-in stands."""
    configs, _ = scan_sources()
    rows = []
    for config in configs:
        cred = creds.status(session, config.slug)
        job = login_manager.status(config.slug)
        rows.append(
            {
                "slug": config.slug,
                "name": config.name,
                "has_login": config.login is not None,
                "cred": cred,
                "session_age": _session_age_hours(config.auth_state),
                "login_job": job,
            }
        )
    return rows


@router.get("/sites", response_class=HTMLResponse)
def sites(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "sites.html",
        {"rows": _site_rows(session), "key_ready": crypto.is_configured()},
    )


@router.post("/sites/{slug}/credentials")
def set_site_credentials(
    slug: str,
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    label: str = Form(""),
    session: Session = Depends(get_session),
):
    """Store an encrypted login. The values are never echoed back or logged."""
    known = {c.slug for c in scan_sources()[0]}
    if slug not in known or not crypto.is_configured():
        return RedirectResponse("/sites", status_code=303)
    creds.set_credentials(session, slug, username.strip(), password, label.strip() or None)
    session.commit()
    # Log the act, never the secret.
    _log(session, request, "set_credentials", {"slug": slug}, None)
    return RedirectResponse("/sites", status_code=303)


@router.post("/sites/{slug}/credentials/delete")
def delete_site_credentials(
    slug: str, request: Request, session: Session = Depends(get_session)
):
    creds.delete_credentials(session, slug)
    session.commit()
    _log(session, request, "delete_credentials", {"slug": slug}, None)
    return RedirectResponse("/sites", status_code=303)


@router.post("/sites/{slug}/login")
def start_login(slug: str, request: Request, session: Session = Depends(get_session)):
    """Kick off a headless sign-in for one site, in the background."""
    known = {c.slug for c in scan_sources()[0]}
    if slug in known:
        started, message = login_manager.start(slug)
        _log(session, request, "login", {"slug": slug, "started": started, "message": message}, None)
    return RedirectResponse("/sites", status_code=303)
