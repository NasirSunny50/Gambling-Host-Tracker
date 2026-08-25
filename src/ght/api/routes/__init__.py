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
from datetime import UTC, datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, func, literal, select, union_all
from sqlalchemy.orm import Session

from ght.api import ASSETS_DIR, TEMPLATES_DIR
from ght.api.jobs import ALL_SITES, manager
from ght.api.schedule import MIN_MINUTES, Scheduler
from ght.config import REPO_ROOT, settings
from ght.db import SessionLocal
from ght.extractors.regex_sweep import page_text
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
from ght.normalize.msisdn import translate_digits
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
    "cellfin": "CellFin",
    "ipay": "iPay",
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


# Everything shown in this portal is read in Bangladesh, about a Bangladeshi site, by
# people who will quote these times to a Bangladeshi bank. A fixed +06:00 rather than a
# named zone: the country has no daylight saving, so the offset is the whole truth and it
# does not depend on a tz database being installed on the machine.
DHAKA = timezone(timedelta(hours=6))


def _stamp(value: datetime | None) -> str:
    """An exact timestamp, in the day-month-year order the reader writes dates in.

    Relative ages read nicely but hide when something happened, which is the thing a case
    file has to state.
    """
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DHAKA).strftime("%d/%m/%Y %I:%M %p")


def _day(value: datetime | None) -> str:
    """Just the date, for a figure caption where the hour would be noise."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DHAKA).strftime("%d/%m/%Y")


templates.env.filters["day"] = _day
templates.env.filters["age"] = _age
templates.env.filters["stamp"] = _stamp
templates.env.filters["channel"] = lambda value: CHANNEL_LABELS.get(value, value)
# The base layout shows a live "collecting…" flag on every page, so the manager is a
# template global rather than something each route has to remember to pass.
templates.env.globals["job"] = manager

# One scheduler for the process, over the same manager the button drives, so a timed
# collection and a hand-started one cannot both be in flight.
scheduler = Scheduler(manager)


# How the newest run's outcome reads in the header. There is no alerts page: whether
# collection is healthy has to be legible from the chrome of whatever page you are on.
_HEALTH = {
    "ok": ("fetching healthy", "ok"),
    "partial": ("fetching degraded", "warn"),
    "failed": ("last run failed", "bad"),
    "blocked": ("site is blocking us", "bad"),
}


def _nav(session: Session) -> dict:
    """What the shell shows on every page. One query, so it costs a page load nothing."""
    latest = session.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
    health, tone = _HEALTH.get(latest.status, (latest.status, "warn")) if latest else ("no fetches yet", "idle")
    return {
        "health": health,
        "health_tone": tone,
        # Local time, to match every timestamp shown in the tables below it.
        "now": datetime.now(DHAKA).strftime("%d/%m/%Y %I:%M %p"),
    }


# Somewhere to drop the payment brands' own logos. Nothing ships here: bKash, Nagad, Upay
# and the banks own their marks, and an approximation drawn from memory would be a
# counterfeit rather than a logo. Put a licensed file at data/branding/<channel>.svg (or
# .png) and the portal uses it wherever that channel appears; without one it falls back to
# the lettered mark, which is what every screenshot in this repo shows.
BRANDING_DIR = REPO_ROOT / "data" / "branding"
BRANDING_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp"}


def _branding_key(name: str) -> str:
    """Fold a name to compare it: case, spaces and hyphens all mean the same thing."""
    return re.sub(r"[\s-]+", "_", name.strip().lower())


def _branding_file(channel: str) -> Path | None:
    """The logo file for a channel, if someone has supplied one.

    Matched on the folded name rather than the exact filename. A logo arrives named the
    way the brand writes it - "Bank transfer.png", "bKash.png" - and asking whoever drops
    it in to also rename it to the channel key is a step that only ever gets forgotten,
    leaving a lettered mark beside a file that is sitting right there.
    """
    if not channel or "/" in channel or "\\" in channel or "." in channel:
        return None
    wanted = _branding_key(channel)
    if not BRANDING_DIR.is_dir():
        return None
    for candidate in sorted(BRANDING_DIR.iterdir()):
        if candidate.suffix.lower() not in BRANDING_TYPES:
            continue
        if candidate.is_file() and _branding_key(candidate.stem) == wanted:
            return candidate
    return None


def channel_logo(channel: str) -> str | None:
    """The URL of a channel's own logo, or None to use the lettered mark."""
    return f"/branding/{channel}" if _branding_file(channel) else None


templates.env.globals["channel_logo"] = channel_logo


@router.get("/logo")
def logo():
    """The product's own logo — the shield-and-scope badge the sidebar shows."""
    return FileResponse(ASSETS_DIR / "logo-badge.png", media_type="image/png")


@router.get("/favicon.png")
def favicon():
    """The same badge, cut down for the browser tab."""
    return FileResponse(ASSETS_DIR / "favicon.png", media_type="image/png")


@router.get("/branding/{channel}")
def branding(channel: str):
    """Serve a supplied brand logo. Only the configured channel names, never a path."""
    path = _branding_file(channel)
    if path is None:
        return HTMLResponse("<h1>404</h1><p>No logo for that channel.</p>", status_code=404)
    return FileResponse(path, media_type=BRANDING_TYPES[path.suffix.lower()])


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    # A payee is a payee. A name-only one has no number to carry to a blocklist, but it is
    # still a party collecting deposits, and leaving it out of the count made a run that
    # found one report that it had found nothing.
    payees = session.scalar(
        select(func.count()).select_from(_payee_query(None, None).order_by(None).subquery())
    ) or 0
    totals = {
        "accounts": payees,
        # What we are tracking, not what we have ever tracked. The sites table is a
        # historical record - a site collected once keeps its rows so its runs and evidence
        # still mean something - so counting it answers a different question than the one
        # the figure asks. The configs on disk are the targets.
        #
        # Counted from the configs alone, never from the dropdown: the dropdown leads with
        # "All sites", which is a choice of target and not a site, and counting the list it
        # sits in reported three tracked sites for the two that exist.
        "sites": len(_tracked_sites()),
        # How much work has gone in. An account count on its own says nothing about
        # whether the collector has been looking - thirty accounts from two runs and from
        # two hundred are very different pictures of a site.
        "runs": session.scalar(select(func.count()).select_from(CollectionRun)) or 0,
    }
    first_run_at = session.scalar(select(func.min(CollectionRun.started_at)))

    # Grouped over the same population the figure counts, so the bars add up to it.
    payee_rows = _payee_query(None, None).order_by(None).subquery()
    by_channel = session.execute(
        select(payee_rows.c.channel, func.count())
        .group_by(payee_rows.c.channel)
        .order_by(func.count().desc())
    ).all()

    runs = session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(8)
    ).all()
    # Newest of either kind, for the same reason.
    newest_rows = _payee_query(None, None).order_by(None).subquery()
    newest = session.execute(
        select(newest_rows).order_by(newest_rows.c.first_seen.desc()).limit(8)
    ).all()
    sites = {s.id: s for s in session.scalars(select(Site))}

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "nav": _nav(session),
            "totals": totals,
            "by_channel": by_channel,
            "first_run_at": first_run_at,
            "runs": runs,
            "newest": newest,
            "sites": sites,
        },
    )


@router.get("/sites", response_class=HTMLResponse)
def sites_page(request: Request, session: Session = Depends(get_session)):
    """What "sites tracked" on the Overview actually means, named one by one.

    The figure alone raises the question it cannot answer - *which* sites? - so it links
    here. The list is the configs on disk, because those are the targets; the database only
    supplies what each one has produced so far, and a config that has never been fetched
    still belongs on the page with nothing beside it.
    """
    tracked = _tracked_sites()
    rows_by_slug = {s.slug: s for s in session.scalars(select(Site))}

    rows = []
    for site in tracked:
        row = rows_by_slug.get(site["slug"])
        stats = {"fetches": 0, "accounts": 0, "names": 0, "last": None}
        if row is not None:
            stats["fetches"] = session.scalar(
                select(func.count()).select_from(CollectionRun).where(CollectionRun.site_id == row.id)
            ) or 0
            stats["accounts"] = session.scalar(
                select(func.count()).select_from(AccountSite).where(AccountSite.site_id == row.id)
            ) or 0
            # Name-only payees are payees too, and a site's total that left them out
            # disagreed with the Payees list filtered to the same site.
            stats["names"] = session.scalar(
                select(func.count()).select_from(
                    select(MerchantSighting.merchant_name, MerchantSighting.channel)
                    .where(MerchantSighting.site_id == row.id)
                    .group_by(MerchantSighting.merchant_name, MerchantSighting.channel)
                    .subquery()
                )
            ) or 0
            stats["last"] = session.scalar(
                select(CollectionRun)
                .where(CollectionRun.site_id == row.id)
                .order_by(CollectionRun.started_at.desc())
                .limit(1)
            )
        rows.append({**site, **stats, "payees": stats["accounts"] + stats["names"]})

    _log(session, request, "api_read", {"sites": len(rows)}, len(rows))
    return templates.TemplateResponse(
        request,
        "sites.html",
        {"nav": _nav(session), "rows": rows},
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


def _accounts_from_run(run_id: int):
    """The accounts one run actually saw.

    Membership comes from the observations that run wrote, not from the accounts table:
    an account is a de-duplicated identity that outlives any single run, and "seen twelve
    times" is the whole point of it. The question here is narrower - which of them did
    *this* run bring in - and only the sightings know that.
    """
    return select(Observation.account_id).where(Observation.run_id == run_id)


def _account_query(channel: str | None, q: str | None, run: int | None = None):
    stmt = select(Account)
    if run:
        stmt = stmt.where(Account.id.in_(_accounts_from_run(run)))
    if channel:
        stmt = stmt.where(Account.channel == channel)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            Account.account_number.like(like)
            | Account.holder_name.like(like)
            | Account.bank_name.like(like)
        )
    return stmt


def _payee_query(channel: str | None, q: str | None, run: int | None = None):
    """Accounts and name-only merchants as one list, newest sighting first.

    They are different things — an account is a de-duplicated identity with a number and a
    status, a merchant is a name that changes on every request — but the question a reader
    brings is the same: who is receiving the deposits. A union keeps them in one ordered,
    pageable list without pretending a merchant has fields it does not.
    """
    # Which site advertised it. Where an account has been seen on several brands the most
    # recent one is shown - the same account across brands is the strongest signal here, and
    # the detail page lists them all.
    account_site = (
        select(Site.slug)
        .join(AccountSite, AccountSite.site_id == Site.id)
        .where(AccountSite.account_id == Account.id)
        .order_by(AccountSite.last_seen_at.desc())
        .limit(1)
        .scalar_subquery()
        .label("site")
    )
    accounts = select(
        Account.id.label("id"),
        literal("account").label("kind"),
        Account.channel.label("channel"),
        Account.account_number.label("number"),
        Account.holder_name.label("name"),
        Account.bank_name.label("bank"),
        Account.confidence.label("confidence"),
        Account.observation_count.label("times"),
        Account.first_seen_at.label("first_seen"),
        Account.last_seen_at.label("last_seen"),
        Account.is_active.label("is_active"),
        Account.needs_review.label("needs_review"),
        account_site,
    )
    if run:
        accounts = accounts.where(Account.id.in_(_accounts_from_run(run)))
    if channel:
        accounts = accounts.where(Account.channel == channel)
    if q:
        like = f"%{q.strip()}%"
        accounts = accounts.where(
            Account.account_number.like(like)
            | Account.holder_name.like(like)
            | Account.bank_name.like(like)
        )

    merchants = (
        select(
            # The newest sighting of this name, which is the one its page opens on.
            func.max(MerchantSighting.id).label("id"),
            literal("merchant").label("kind"),
            MerchantSighting.channel.label("channel"),
            literal(None, String).label("number"),
            MerchantSighting.merchant_name.label("name"),
            literal(None, String).label("bank"),
            literal(None).label("confidence"),
            func.count().label("times"),
            func.min(MerchantSighting.seen_at).label("first_seen"),
            func.max(MerchantSighting.seen_at).label("last_seen"),
            literal(None).label("is_active"),
            literal(None).label("needs_review"),
            func.min(Site.slug).label("site"),
        )
        .join(Site, Site.id == MerchantSighting.site_id)
        .group_by(MerchantSighting.merchant_name, MerchantSighting.channel)
    )
    if run:
        merchants = merchants.where(MerchantSighting.run_id == run)
    if channel:
        merchants = merchants.where(MerchantSighting.channel == channel)
    if q:
        merchants = merchants.where(MerchantSighting.merchant_name.like(f"%{q.strip()}%"))

    combined = union_all(accounts, merchants).subquery()
    return select(combined).order_by(combined.c.last_seen.desc())


def sites_by_id(session: Session) -> dict:
    return {s.id: s for s in session.scalars(select(Site))}


@router.get("/payees", response_class=HTMLResponse)
def payees(
    request: Request,
    channel: str | None = None,
    q: str | None = None,
    run: int | None = None,
    page: int = 1,
    per: int = PAGE_SIZE,
    session: Session = Depends(get_session),
):
    """One list of every payee the fetcher has brought back."""
    per_page = per if per in PAGE_SIZES else PAGE_SIZE
    stmt = _payee_query(channel, q, run)
    rows, page_info = _paginate(session, stmt, page, per_page=per_page, scalar=False)
    _log(
        session,
        request,
        "search",
        {"channel": channel, "q": q, "run": run, "page": page_info.number},
        page_info.total,
    )

    channels = sorted(
        set(session.scalars(select(Account.channel).distinct()).all())
        | set(session.scalars(select(MerchantSighting.channel).distinct()).all())
    )
    # Filtering to a run is the one filter that is not a control on this page - it arrives
    # from the run that just finished - so the page has to say whose list this is.
    run_row = session.get(CollectionRun, run) if run else None
    return templates.TemplateResponse(
        request,
        "payees.html",
        {
            "nav": _nav(session),
            "rows": rows,
            "pagination": page_info,
            "channels": channels,
            "channel": channel,
            "q": q or "",
            "run": run,
            "run_row": run_row,
            "run_site": sites_by_id(session).get(run_row.site_id) if run_row else None,
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


def _probe_of(blob: Evidence) -> str:
    """Which probe produced a stored blob. Paths are <slug>/<probe>/<xx>/<sha>.<ext>."""
    parts = (blob.path or "").split("/")
    return parts[1] if len(parts) > 2 else ""


def _screenshot_of_run(session: Session, run_id: int, probe: str) -> Evidence | None:
    """The screenshot from one probe of one run."""
    blobs = session.scalars(
        select(Evidence)
        .where(Evidence.run_id == run_id, Evidence.kind == "screenshot")
        .order_by(Evidence.id.desc())
    ).all()
    return next((b for b in blobs if _probe_of(b) == probe), None)


@router.get("/merchants/{sighting_id}", response_class=HTMLResponse)
def merchant_detail(sighting_id: int, request: Request, session: Session = Depends(get_session)):
    """A name-only payee, and the page it was named on.

    These never carry a number, so there is nothing to look up in the accounts table and
    nothing to copy onto a blocklist. What there is, is a picture of the checkout that
    named them - which is the whole of the evidence for a payee of this kind, and until now
    was captured on every run and shown to nobody.
    """
    sighting = session.get(MerchantSighting, sighting_id)
    if sighting is None:
        return HTMLResponse("<h1>404</h1><p>No such payee.</p>", status_code=404)

    # Every sighting of this same name on this channel: the name rotates per request, so
    # the history is the point rather than a single row.
    sightings = session.scalars(
        select(MerchantSighting)
        .where(
            MerchantSighting.merchant_name == sighting.merchant_name,
            MerchantSighting.channel == sighting.channel,
        )
        .order_by(MerchantSighting.seen_at.desc())
    ).all()

    newest = sightings[0]
    _log(session, request, "api_read", {"merchant": sighting.merchant_name}, 1)
    return templates.TemplateResponse(
        request,
        "merchant_detail.html",
        {
            "nav": _nav(session),
            "merchant": newest,
            "sightings": sightings,
            "site": session.get(Site, newest.site_id),
            "screenshot": _screenshot_of_run(session, newest.run_id, newest.probe),
        },
    )


def _number_forms(account: Account, sighting: Observation | None) -> list[str]:
    """Every shape this account's number could take on a page.

    What is stored is canonical - ``+8801XXXXXXXXX`` - and what a site prints is not. It
    prints the national form, sometimes with spaces in it, sometimes in Bengali numerals,
    and for a Rocket wallet with a check digit on the end. Searching a page for the
    canonical string therefore found nothing on almost every mobile wallet.
    """
    forms = []
    if sighting is not None and sighting.raw_text:
        forms.append(sighting.raw_text)
    number = account.account_number or ""
    if number:
        forms.append(number)
        if number.startswith("+880"):
            forms.append("0" + number[4:])
    return forms


def _page_shows(body: str, forms: list[str]) -> bool:
    """Whether this stored page is one that actually published the number.

    Read from the page's visible text rather than its markup, so a digit sequence that
    happens to sit inside an attribute or a script cannot pass for a published payee. The
    digits-only comparison at the end is for the pages that print a number with spaces
    through it.
    """
    text = translate_digits(page_text(body))
    if any(form in text for form in forms):
        return True
    digits = re.sub(r"\D", "", text)
    return any(
        len(bare := re.sub(r"\D", "", form)) >= 9 and bare in digits for form in forms
    )


def _screenshot_for(session: Session, account: Account) -> Evidence | None:
    """The picture of this number on the site, from the last fetch that saw it.

    A page of hashes proves the number was published; a screenshot *shows* it, which is
    what a reviewer who does not read HTML actually needs. Which one matters: a fetch
    captures every method, and the screenshot has to come from the method whose page
    carried *this* number. Evidence paths are ``<slug>/<probe>/<xx>/<sha>.<ext>``, so the
    stored page that shows the number names the probe, and the screenshot beside it in the
    same probe folder is the picture of that page.

    Returns None when no stored page can be shown to have published it. Showing some other
    method's screenshot instead would put one payee's evidence on another payee's page,
    which is worse than showing nothing at all.
    """
    latest = session.scalar(
        select(Observation)
        .where(Observation.account_id == account.id)
        .order_by(Observation.observed_at.desc())
        .limit(1)
    )
    if latest is None:
        return None

    # Oldest first: a closed modal leaves its markup behind, so a later method's page can
    # still carry the previous one's number. The first page to show it is the one that
    # opened it.
    blobs = session.scalars(
        select(Evidence).where(Evidence.run_id == latest.run_id).order_by(Evidence.id)
    ).all()
    shots = [b for b in blobs if b.kind == "screenshot"]
    if not shots:
        return None

    forms = _number_forms(account, latest)
    for html_blob in (b for b in blobs if b.kind == "html"):
        try:
            body = (settings.evidence_dir / html_blob.path).read_text(
                encoding="utf-8", errors="ignore"
            )
        except OSError:
            continue
        if _page_shows(body, forms):
            probe = _probe_of(html_blob)
            match = next((s for s in shots if _probe_of(s) == probe), None)
            if match is not None:
                return match
    return None


@router.get("/evidence/{evidence_id}.png")
def evidence_image(evidence_id: int, request: Request, session: Session = Depends(get_session)):
    """Serve one stored screenshot.

    Addressed by database id, never by a path from the URL: the id is looked up, and the
    file it names is required to resolve inside the evidence directory. A portal that
    accepted a path here would hand out any file on the machine to anyone who reached it.
    """
    blob = session.get(Evidence, evidence_id)
    if blob is None or blob.kind != "screenshot":
        return HTMLResponse("<h1>404</h1><p>No such screenshot.</p>", status_code=404)

    root = settings.evidence_dir.resolve()
    path = (root / blob.path).resolve()
    if not path.is_relative_to(root) or not path.exists():
        return HTMLResponse("<h1>404</h1><p>The stored file is missing.</p>", status_code=404)
    return FileResponse(path, media_type="image/png")


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

    _log(session, request, "api_read", {"account_id": account_id}, 1)
    return templates.TemplateResponse(
        request,
        "account_detail.html",
        {
            "nav": _nav(session),
            "account": account,
            "observations": observations,
            "sites": sites,
            "screenshot": _screenshot_for(session, account),
        },
    )


def _base_url(url: str) -> str:
    """Just the site's address - scheme and host, no path."""
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else url


def _tracked_sites() -> list[dict]:
    """The sites this install is configured to collect - one per config file, nothing else.

    Kept apart from ``_runnable_sites`` because the two answer different questions. This
    one is "what are we tracking", which is what the Overview figure counts and the Sites
    page lists; the other adds the "All sites" choice, which is a way of pointing a fetch
    rather than a site of its own.
    """
    configs, broken = scan_sources()
    sites = [
        {
            "slug": config.slug,
            "name": config.name,
            "status": config.status,
            "fetcher": config.fetcher,
            # The address being collected, so a reader can see what the slug stands for.
            # The current deposit URL if the config marks one, else whatever it lists
            # first - these operators rotate domains, so a config carries the mirrors too.
            # Cut back to the host: the deposit path is a detail of how we collect, and a
            # full URL down to /office/recharge pushed the column wide for nothing.
            "url": _base_url(next(
                (u.url for u in config.urls if u.current),
                config.urls[0].url if config.urls else "",
            )),
            "broken": False,
        }
        for config in configs
    ]
    # A config this process cannot parse must not make its site vanish from the dropdown.
    # The usual cause is not a real typo but a portal still holding older code in memory
    # after the config gained a field it does not know yet - and a fetch runs in a fresh
    # subprocess that parses the file fine. So a broken file is still offered, keyed by its
    # filename, rather than silently dropped.
    known = {site["slug"] for site in sites}
    for bad in broken:
        slug = bad.path.stem
        if slug not in known:
            sites.append({
                "slug": slug, "name": slug, "status": "active", "fetcher": "",
                "url": "", "broken": True,
            })
    return sites


def _runnable_sites() -> list[dict]:
    """The sites a fetch can be pointed at, with "every one of them" offered first.

    "All" is a choice of target rather than a second kind of fetch, so it belongs in the
    same dropdown. It runs the active sites one after another - never at once, because a
    fetch drives a real browser and two of them race on the login session.
    """
    sites = _tracked_sites()
    # "All" is decided *after* the broken files are folded back in, and counted the same way
    # a fetch would run them. A file that fails to parse in this long-lived process still
    # parses fine in the fresh subprocess a fetch spawns, so "all" would fetch it - and the
    # choice must not disappear from the dropdown just because the portal has not been
    # restarted yet. So the count is every runnable target, not only the ones this process
    # could parse.
    active = [s for s in sites if s["status"] == "active"]
    everything = [
        {
            "slug": ALL_SITES,
            "name": f"All sites ({len(active)})" if active else "All sites",
            "status": "active",
            "fetcher": "",
        }
    ] if len(active) > 1 else []
    return everything + sites


def _is_waiting(info) -> bool:
    """Whether the run is paused for the operator rather than working.

    This is the one state the page must never let read as "busy" or as "broken": the run
    is fine, it is standing still until a person signs in to a window that opened
    elsewhere. The progress messages that mean it are the ones auth_login emits.
    """
    message = (info.message or "").lower()
    return any(
        phrase in message
        for phrase in ("waiting for you", "opening a browser window", "opening a window for you")
    )


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
def runs(
    request: Request,
    page: int = 1,
    per: int = PAGE_SIZE,
    session: Session = Depends(get_session),
):
    # The history is paged like every other list rather than cut off at a hundred. A
    # truncated table answers "what happened lately" and quietly refuses "what happened on
    # the 3rd" - and this is the table an operator goes back through when a number needs a
    # date attached to it.
    per_page = per if per in PAGE_SIZES else PAGE_SIZE
    rows, run_page = _paginate(
        session,
        select(CollectionRun).order_by(CollectionRun.started_at.desc()),
        page,
        per_page=per_page,
    )
    sites = {s.id: s for s in session.scalars(select(Site))}

    running = manager.is_running
    info = manager.current
    waiting = bool(info) and running and _is_waiting(info)

    # The outcome card is shown once and then stood down; reloading is how the operator
    # says they have read it. Everything it said stays in the history table below.
    finished = None if running else manager.take_finished()
    shown = info if running else finished

    phases = shown.phases if shown else []
    # A phase the operator is blocking is not "in progress" — it is waiting on them, and
    # the checklist has to say so rather than spinning as though work were happening.
    if waiting:
        phases = [{**p, "state": "waiting" if p["state"] == "active" else p["state"]} for p in phases]

    # The run row the finished summary describes: the newest one for that site. Read from
    # the database rather than from the rows on screen - the history is paged now, and an
    # operator reading page three has just as much right to the card as one on page one.
    last_run = None
    if finished:
        newest = select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1)
        if finished.slug != ALL_SITES:
            newest = newest.join(Site, Site.id == CollectionRun.site_id).where(
                Site.slug == finished.slug
            )
        last_run = session.scalar(newest)

    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "nav": _nav(session),
            "rows": rows,
            "p": run_page,
            "per": per_page,
            "page_sizes": PAGE_SIZES,
            "sites": sites,
            "runnable": _runnable_sites(),
            "job_running": running,
            "job_log": manager.log_tail if finished else [],
            "waiting": waiting,
            "seconds_left": _seconds_left(info) if waiting else None,
            "phases": phases,
            # Which of the two lanes shows this fetch: the one that started it. A fetch the
            # schedule fired stays on the schedule side rather than taking over the button's
            # column, so "did I start this?" is answered by where it is.
            "run_source": getattr(shown, "source", "manual") if shown else "manual",
            "elapsed": _elapsed(shown) if shown else "",
            "finished": finished,
            "last_run": last_run,
            "schedule": scheduler.state,
            "schedule_seconds": scheduler.seconds_until_next,
            "schedule_per_day": scheduler.runs_per_day,
            "schedule_min_minutes": MIN_MINUTES,
            # While a run is in flight the page reloads itself so progress is visible
            # without the analyst hammering refresh.
            "auto_refresh": manager.is_running,
        },
    )


def _duration(start: datetime | None, end: datetime | None) -> str:
    """How long a run took, or an honest blank when it never recorded an end.

    A run killed mid-flight keeps its start and never gets a finish, and showing "0s" for
    that would read as a run that did nothing rather than one nobody knows the end of.
    """
    if start is None or end is None:
        return ""
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return ""
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _first_seen_in(session: Session, run_id: int) -> tuple[set[int], set[tuple]]:
    """The payees this fetch was the first ever to see.

    Asked of the sightings rather than of the fetch's own ``accounts_new`` counter: that
    counter was written when the fetch ran and counts accounts only, while the question on
    the page is which rows in front of the reader are new - name-only payees included.

    A payee is new here if no earlier fetch ever recorded it. The comparison is by first
    sighting, so re-reading history gives the same answer it gave on the day.
    """
    earliest_account = (
        select(Observation.account_id, func.min(Observation.run_id).label("first_run"))
        .group_by(Observation.account_id)
        .subquery()
    )
    accounts = {
        row[0]
        for row in session.execute(
            select(earliest_account.c.account_id).where(earliest_account.c.first_run == run_id)
        )
    }

    earliest_merchant = (
        select(
            MerchantSighting.merchant_name.label("name"),
            MerchantSighting.channel.label("channel"),
            func.min(MerchantSighting.run_id).label("first_run"),
        )
        .group_by(MerchantSighting.merchant_name, MerchantSighting.channel)
        .subquery()
    )
    merchants = {
        (row[0], row[1])
        for row in session.execute(
            select(earliest_merchant.c.name, earliest_merchant.c.channel).where(
                earliest_merchant.c.first_run == run_id
            )
        )
    }
    return accounts, merchants


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, session: Session = Depends(get_session)):
    """One run: when it went, how it ended, what it brought back, what it stored.

    The history table answers "did it work"; this answers "what happened". Both matter,
    and cramming the second into a row of the first is how the note column ended up
    carrying a sentence nobody could read.
    """
    run = session.get(CollectionRun, run_id)
    if run is None:
        return HTMLResponse("<h1>404</h1><p>No such run.</p>", status_code=404)

    # The payees this run actually brought back, read the same way the Payees page reads
    # them - so the list here and the list behind "view what this run collected" cannot
    # disagree with each other.
    payees = session.execute(_payee_query(None, None, run_id)).all()

    new_accounts, new_merchants = _first_seen_in(session, run_id)

    def is_new(row) -> bool:
        if row.kind == "account":
            return row.id in new_accounts
        return (row.name, row.channel) in new_merchants

    # Counted off the same rows the list marks, so the figure and the marks cannot disagree.
    new_count = sum(1 for row in payees if is_new(row))

    _log(session, request, "api_read", {"run_id": run_id}, len(payees))
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "nav": _nav(session),
            "run": run,
            "site": session.get(Site, run.site_id),
            "payees": payees,
            "duration": _duration(run.started_at, run.finished_at),
            "new_accounts": new_accounts,
            "new_merchants": new_merchants,
            "new_count": new_count,
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
    """Kick off a fetch in the background. Only known site slugs, or "all", are accepted."""
    known = {s["slug"] for s in _runnable_sites()}
    if slug not in known:
        return RedirectResponse("/runs", status_code=303)

    started, message = manager.start(slug)
    _log(session, request, "run", {"slug": slug, "started": started, "message": message}, None)
    return RedirectResponse("/runs", status_code=303)


@router.post("/schedule")
def start_schedule(
    request: Request,
    slug: str = Form(...),
    minutes: int = Form(...),
    session: Session = Depends(get_session),
):
    """Put a site, or all of them, on an interval. Same targets as starting one by hand."""
    if slug not in {s["slug"] for s in _runnable_sites()}:
        return RedirectResponse("/runs", status_code=303)

    started, message = scheduler.start(slug, minutes)
    _log(
        session,
        request,
        "run",
        {"schedule": "start", "slug": slug, "minutes": minutes, "started": started,
         "message": message},
        None,
    )
    return RedirectResponse("/runs", status_code=303)


@router.post("/schedule/stop")
def stop_schedule(request: Request, session: Session = Depends(get_session)):
    scheduler.stop()
    _log(session, request, "run", {"schedule": "stop"}, None)
    return RedirectResponse("/runs", status_code=303)


def _merchant_rows(channel: str | None, q: str | None, run: int | None):
    """Name-only payees, grouped the way the Payees table groups them."""
    stmt = (
        select(
            MerchantSighting.merchant_name,
            MerchantSighting.channel,
            func.count().label("times"),
            func.min(MerchantSighting.seen_at),
            func.max(MerchantSighting.seen_at),
        )
        .join(Site, Site.id == MerchantSighting.site_id)
        .group_by(MerchantSighting.merchant_name, MerchantSighting.channel)
        .order_by(MerchantSighting.channel)
    )
    if run:
        stmt = stmt.where(MerchantSighting.run_id == run)
    if channel:
        stmt = stmt.where(MerchantSighting.channel == channel)
    if q:
        stmt = stmt.where(MerchantSighting.merchant_name.like(f"%{q.strip()}%"))
    return stmt


@router.get("/payees.pdf")
def payees_pdf(
    request: Request,
    channel: str | None = None,
    q: str | None = None,
    run: int | None = None,
    session: Session = Depends(get_session),
):
    """The same payees as a document rather than a data file.

    The CSV is for a system to read; this is for a person to file, initial, or attach to a
    case. So it carries the things a loose printed page needs to still mean something: what
    it is a report of, when it was taken, by whom, and a page number on every sheet.
    """
    rows = [dict(r._mapping) for r in session.execute(_payee_query(channel, q, run))]
    run_row = session.get(CollectionRun, run) if run else None
    scope = _describe_scope(channel, q, run_row)
    _log(session, request, "export", {"format": "pdf", "channel": channel, "q": q, "run": run},
         len(rows))

    try:
        from ght.export.report import build_pdf
    except ImportError:
        return HTMLResponse(
            "<h1>PDF export is not installed</h1>"
            '<p>Install the export extra: <code>pip install -e ".[export]"</code></p>',
            status_code=501,
        )

    pdf = build_pdf(rows, scope=scope, actor=_actor(request), channel_labels=CHANNEL_LABELS)
    stamp = datetime.now(DHAKA).strftime("%Y%m%d-%H%M")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="payees-{stamp}.pdf"'},
    )


def _describe_scope(channel: str | None, q: str | None, run_row) -> str:
    """The filters behind a report, named the way the page names them.

    Each filter says which control it came from - Channel, Search, Fetch - rather than
    running the values together, because a reader holding the printed page cannot see the
    form that produced it. A report with no filters says so outright: a blank there would
    read as a filter nobody wrote down.
    """
    parts = []
    if run_row is not None:
        parts.append(f"Fetch #{run_row.id}")
    if channel:
        parts.append(f"Channel: {CHANNEL_LABELS.get(channel, channel)}")
    if q:
        parts.append(f'Search: "{q.strip()}"')
    return " · ".join(parts) if parts else "None — every payee, every site"


@router.get("/accounts.csv")
def accounts_csv(
    request: Request,
    channel: str | None = None,
    q: str | None = None,
    run: int | None = None,
    session: Session = Depends(get_session),
):
    """The same rows the table is showing, as a file the AML team can hand on.

    Both kinds of payee, because both are what the table shows. A name-only one has no
    number to blocklist, but leaving it out of the export would mean the file quietly
    disagreed with the screen it was downloaded from — and the names are the whole of what
    is collectable from the methods that publish no wallet.
    """
    rows = session.scalars(_account_query(channel, q, run).order_by(Account.channel)).all()
    merchants = session.execute(_merchant_rows(channel, q, run)).all()
    _log(
        session,
        request,
        "export",
        {"channel": channel, "q": q, "run": run},
        len(rows) + len(merchants),
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "kind",
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
                "account",
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
    for name, merchant_channel, times, first_seen, last_seen in merchants:
        writer.writerow(
            [
                "name_only",
                merchant_channel,
                "",  # there is no number; the method never publishes one
                name,
                "",
                "",
                "",
                "",
                "",
                # A name is never marked gone: it rotates per deposit request, so its
                # absence today says nothing about whether it is still in use.
                "",
                times,
                first_seen.isoformat() if first_seen else "",
                last_seen.isoformat() if last_seen else "",
            ]
        )

    buffer.seek(0)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="accounts-{stamp}.csv"'},
    )

