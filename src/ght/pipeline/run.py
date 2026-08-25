"""Orchestration: one collection run against one site."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ght.extractors.base import ExtractionResult, extract
from ght.fetchers import get_fetcher, looks_blocked
from ght.models import (
    Alert,
    CollectionRun,
    Evidence,
    MerchantSighting,
    Site,
    SiteUrl,
    utcnow,
)
from ght.pipeline.changeset import ChangeSet, compute_changeset, refresh_active_flags
from ght.pipeline.dedup import record_observations
from ght.pipeline.evidence import store_capture
from ght.progress import ProgressFn
from ght.progress import report as emit_progress
from ght.sources import Block, Probe, SourceConfig
from ght.types import RawCapture


@dataclass
class RunReport:
    slug: str
    status: str
    url: str | None = None
    run_id: int | None = None
    http_status: int | None = None
    error: str | None = None
    flow_error: str | None = None
    extraction: ExtractionResult | None = None
    changes: ChangeSet = field(default_factory=ChangeSet)
    evidence_paths: list[str] = field(default_factory=list)
    # Name-only payees seen this run; they have no account row to point at.
    merchants: list[str] = field(default_factory=list)

    @property
    def account_count(self) -> int:
        return len(self.extraction.accounts) if self.extraction else 0


def sync_site(session: Session, config: SourceConfig) -> Site:
    """Make the DB reflect the YAML config for this site."""
    site = session.scalar(select(Site).where(Site.slug == config.slug))
    if site is None:
        site = Site(slug=config.slug, name=config.name, status=config.status, notes=config.notes)
        session.add(site)
        session.flush()
    else:
        site.name = config.name
        site.status = config.status
        site.notes = config.notes

    known = {
        url.url: url for url in session.scalars(select(SiteUrl).where(SiteUrl.site_id == site.id))
    }
    for entry in config.urls:
        row = known.get(entry.url)
        if row is None:
            session.add(
                SiteUrl(
                    site_id=site.id,
                    url=entry.url,
                    url_type=entry.type,
                    is_current=entry.current,
                )
            )
        else:
            row.url_type = entry.type
            row.is_current = entry.current
    session.flush()
    return site


def _fetcher_kwargs(config: SourceConfig) -> dict:
    """Browser-only options from the site config. The HTTP fetcher takes none of these."""
    if config.fetcher != "browser":
        return {}
    kwargs = {
        "flow": config.flow,
        "wait_for": config.wait_for,
        "auth_state": config.auth_state,
        "channel": config.browser_channel,
        "frame": config.frame,
        "logged_out_marker": config.logged_out_marker,
        "session_expired": config.session_expired,
        "unavailable": config.unavailable,
        "reset": config.reset,
        "shot": config.shot,
    }
    if config.timeout is not None:
        kwargs["timeout"] = config.timeout
    if config.flow_timeout is not None:
        kwargs["flow_timeout"] = config.flow_timeout
    return kwargs


def fetch_first_working_url(config: SourceConfig) -> tuple[RawCapture, str | None]:
    """Try the current URL, then fall back through known mirrors.

    These operators rotate domains without warning, so a dead primary is routine rather
    than exceptional. Returns the capture along with the configured URL that produced it,
    which is not the same as ``capture.url`` once redirects are involved.
    """
    fetcher = get_fetcher(config.fetcher, **_fetcher_kwargs(config))
    last: RawCapture | None = None
    last_url: str | None = None

    for url in config.current_urls:
        capture = fetcher.fetch(url)
        if capture.ok and not looks_blocked(capture):
            return capture, url
        last, last_url = capture, url

    if last is None:
        return (
            RawCapture(url="", status_code=0, fetcher=config.fetcher, error="no urls configured"),
            None,
        )
    return last, last_url


def _merchant_name(html: str, selector: str) -> str | None:
    """The payee name a merchant probe points at, if the element is there."""
    from selectolax.parser import HTMLParser

    node = HTMLParser(html).css_first(selector)
    if node is None:
        return None
    return " ".join(node.text(separator=" ", strip=True).split()) or None


def probes_of(config: SourceConfig) -> list[Probe]:
    """The probes to run, treating a plain single-flow source as one unnamed probe.

    Probes marked ``creates_order`` reach the payee by confirming a deposit, which raises a
    deposit *request* on the operator — no funds move and nothing is paid, but the request
    is initiated every run. The flag is kept as metadata so the portal can show which
    probes do this; it is not filtered out, because collecting those payees is the point.

    A probe that names ``requires_env`` is skipped while that variable is unset. It types a
    value that lives outside git - a payer's phone number - and for a creates_order probe
    skipping it also means no order is raised, so it stays off until the operator opts in by
    setting the number.
    """
    from ght.credentials import env_value

    probes = config.probes or [
        Probe(name=config.slug, flow=config.flow, wait_for=config.wait_for, blocks=config.blocks)
    ]
    return [p for p in probes if not p.requires_env or env_value(p.requires_env)]


def config_for_probe(config: SourceConfig, probe: Probe) -> SourceConfig:
    """The source config as this probe sees it: its own flow, its own selectors."""
    return config.model_copy(
        update={
            "flow": probe.flow,
            "wait_for": probe.wait_for or config.wait_for,
            "blocks": probe.blocks,
            "probes": [],
        }
    )


def _capture_status(capture: RawCapture) -> str:
    if capture.error:
        return "failed"
    if looks_blocked(capture):
        return "blocked"
    if not capture.ok:
        return "failed"
    return "ok"


def _looks_logged_out(config: SourceConfig, capture: RawCapture) -> bool:
    """Whether a capture shows an expired session rather than the deposit page.

    Three independent signals, any one is enough: the fetcher flagged the capture as logged
    out, the body carries the logged-out marker, or the request was redirected to the site's
    login page. The URL signal matters because a login page shares no markup with the
    deposit page, so a body marker taken from the logged-out homepage will not match it.

    The fetcher's own flag stands on its own rather than only confirming a marker. It is
    raised where the fetcher has proof — the logged-out layout, or the payment app's own
    refusal dialog — and a site configured with no body marker, as Melbet is, would
    otherwise have that proof thrown away.
    """
    if capture.flow_error == "LOGGED_OUT":
        return True
    marker = config.logged_out_marker
    if marker and marker in capture.text:
        return True
    url_marker = config.logged_out_url
    return bool(url_marker and url_marker in (capture.url or ""))


@dataclass(frozen=True)
class _Plan:
    """One probe as the fetcher needs to see it, for a shared-panel pass."""

    name: str
    flow: list
    wait_for: str | None
    # Confirming a deposit hands off to the provider's own site, which leaves nothing
    # behind to reset - whatever follows has to start from a fresh page.
    ends_navigation: bool


def _collect_in_one_visit(
    config: SourceConfig, probes: list[Probe], on_progress: ProgressFn | None
) -> tuple[list, str | None, bool] | None:
    """Walk every probe inside one loaded panel, or None if this source cannot.

    Loading the embedded panel costs ten to thirteen seconds and every probe needs the same
    one, so re-fetching it per probe was the bulk of a run. Only browser sources with a
    configured reset can do this: without a proven way back to the method list, sharing the
    panel risks one method's modal answering the next method's question.
    """
    # Worth sharing the panel for more than one probe, or for any discovery - a discovery
    # walks several methods through the same loaded panel just as probes do.
    enough_to_share = len(probes) >= 2 or bool(config.discover)
    if config.fetcher != "browser" or config.reset is None or not enough_to_share:
        return None

    fetcher = get_fetcher(config.fetcher, **_fetcher_kwargs(config))
    if not hasattr(fetcher, "fetch_many"):
        return None

    urls = list(config.current_urls)
    plans = []
    configs = []
    for probe in probes:
        probe_config = config_for_probe(config, probe)
        configs.append(probe_config)
        plans.append(
            _Plan(
                name=probe.name,
                flow=probe.flow,
                wait_for=probe_config.wait_for,
                ends_navigation=probe.creates_order,
            )
        )

    total = len(plans)
    emit_progress(on_progress, "collect", f"Opening the deposit panel ({total} methods)", step=0, total=total)
    results, discovered = fetcher.fetch_many(urls, plans, config.discover)

    captures = []
    auth_expired = False
    for index, (probe, probe_config, capture) in enumerate(zip(probes, configs, results), start=1):
        emit_progress(on_progress, "collect", f"Read {probe.name}", step=index, total=total)
        captures.append((probe, probe_config, capture))
        if _looks_logged_out(config, capture):
            auth_expired = True

    for found in discovered:
        emit_progress(on_progress, "collect", f"Found {found.name}")
        captures.append(_discovered_capture(config, found))
        if _looks_logged_out(config, found.capture):
            auth_expired = True
    return captures, (urls[0] if urls else None), auth_expired


def _evidence_name(probe_name: str) -> str:
    """A filesystem-safe folder name for a probe's evidence.

    Hand-written probe names are already safe, but a discovered method is named for what it
    is - "e-wallet: Fast Nagad", "bank: The city bank limited" - and the colon in that is an
    illegal path character on Windows, which crashed the whole run at evidence-storing time.
    The display name is kept as it is; only the folder is folded to letters, digits and
    hyphens."""
    safe = re.sub(r"[^A-Za-z0-9]+", "-", probe_name).strip("-").lower()
    return safe or "probe"


def _discovered_capture(config: SourceConfig, found) -> tuple:
    """Turn one run-time discovery into the probe/config/capture triple extraction reads.

    A discovered method was never named in config, so the block that reads it is built here
    from what the discovery worked out - the channel a cell's name implied, or the bank an
    option named - which is exactly what a hand-written probe's block would have carried."""
    block = Block(
        channel=found.channel,
        value=found.value,
        container=found.container,
        holder=found.holder,
        account_type=found.account_type,
        bank_name=found.bank_name,
    )
    probe = Probe(name=found.name, blocks=[block])
    return probe, config_for_probe(config, probe), found.capture


def _collect_captures(
    config: SourceConfig, on_progress: ProgressFn | None = None
) -> tuple[list, str | None, bool]:
    """Fetch every probe, stopping early if a dead login lands them on the logged-out page.

    Returns the captures, the configured URL that answered, and whether the session looked
    expired. A dead login lands every probe on the same wall, so detecting it on the first
    probe and stopping avoids running all ten into it.
    """
    captures = []
    source_url: str | None = None
    auth_expired = False
    probes = probes_of(config)
    total = len(probes)

    shared = _collect_in_one_visit(config, probes, on_progress)
    if shared is not None:
        return shared

    for index, probe in enumerate(probes, start=1):
        emit_progress(on_progress, "collect", f"Reading {probe.name}", step=index, total=total)
        probe_config = config_for_probe(config, probe)
        probe_capture, probe_url = fetch_first_working_url(probe_config)
        captures.append((probe, probe_config, probe_capture))
        if source_url is None:
            source_url = probe_url
        if _looks_logged_out(config, probe_capture):
            auth_expired = True
            emit_progress(on_progress, "collect", "The site signed us out", step=index, total=total)
            break
    return captures, source_url, auth_expired


def _sign_in(config: SourceConfig, on_progress: ProgressFn | None = None) -> tuple[bool, str]:
    """Establish a live session before collecting, and report whether it worked.

    A run starts here rather than discovering halfway through that the session died. If the
    saved session still works this returns in a couple of seconds. Otherwise the sign-in is
    attempted unattended from credentials in the environment, and only a site that answers
    with a CAPTCHA or 2FA brings up a window for the operator - who solves that themselves.
    """
    if config.login is None:
        return False, "no login flow is configured"

    from ght.auth_login import perform_login

    result = perform_login(config, on_progress=on_progress)
    if result.ok:
        return True, result.detail or "signed in"
    if result.reason == "timeout":
        return False, "the sign-in window was not completed in time"
    if result.reason == "challenge":
        # Only reachable on a site not marked assisted: an assisted one answers a challenge
        # by opening the window instead of giving up here.
        return False, "a CAPTCHA or 2FA appeared; this site needs assisted login (assisted: true)"
    return False, f"sign-in failed: {result.detail or result.reason}"


def run_site(
    session: Session,
    config: SourceConfig,
    dry_run: bool = False,
    on_progress: ProgressFn | None = None,
) -> RunReport:
    """Sign in, fetch, extract, and persist one site's deposit accounts."""
    # When the run began, taken before any work rather than when the row gets written. The
    # row cannot be created until the first capture has answered - it carries the URL and
    # the outcome - and stamping it then recorded the *end* of collection as its start,
    # which made every run in the history look instantaneous.
    started_at = utcnow()
    site = sync_site(session, config)

    # Sign in before collecting rather than after failing. When the session is still good
    # this costs a couple of seconds; when it is not, the operator deals with it once, up
    # front, instead of watching every probe fail. A dry run stays side-effect-free.
    auto_login_note = ""
    if config.login is not None and not dry_run:
        emit_progress(on_progress, "signin", "Checking the site sign-in")
        signed_in, auto_login_note = _sign_in(config, on_progress)
        emit_progress(
            on_progress,
            "signin",
            auto_login_note if signed_in else f"Could not sign in: {auto_login_note}",
        )
        if signed_in:
            session.add(
                Alert(type="auth_refreshed", site_id=site.id, payload={"note": auto_login_note})
            )

    captures, source_url, auth_expired = _collect_captures(config, on_progress)

    # The first probe decides whether the site itself is reachable. A later probe failing
    # is a per-method problem and must not be reported as the site being down.
    capture = captures[0][2]
    status = _capture_status(capture)

    run = CollectionRun(
        site_id=site.id,
        url=capture.url or None,
        fetcher=config.fetcher,
        status=status,
        http_status=capture.status_code or None,
        error=capture.error,
        started_at=started_at,
    )
    session.add(run)
    session.flush()

    report = RunReport(
        slug=config.slug,
        status=status,
        url=capture.url or None,
        run_id=run.id,
        http_status=capture.status_code or None,
        error=capture.error,
        flow_error=capture.flow_error,
    )

    if auth_expired:
        # The fetch itself succeeded (a 200 logged-out page), so this is neither site_down
        # nor a stale selector. Name it for what it is and say how to fix it.
        if auto_login_note:
            # Recovery was tried and did not get us back in.
            message = (
                f"Login session expired and sign-in did not recover it: {auto_login_note}. "
                "Run the collection again and complete the sign-in in the window it opens."
            )
        else:
            message = (
                "Login session expired. Run the collection again and complete the "
                "sign-in in the window it opens."
            )
        run.status = "failed"
        run.error = message
        run.finished_at = utcnow()
        report.status = "failed"
        report.error = message
        # Report it against sign-in, not against the probe that tripped over it. The
        # session is what failed; collection only discovered it. A checklist that ticks
        # "signed in" and then fails later describes something that never happened.
        emit_progress(on_progress, "signin", "The saved session was not valid", ok=False)
        session.add(Alert(type="auth_expired", site_id=site.id, payload={"url": capture.url}))
        return report

    if status != "ok":
        run.finished_at = utcnow()
        emit_progress(
            on_progress,
            "collect",
            "The site was blocking us" if status == "blocked" else "The site did not load",
            ok=False,
        )
        session.add(
            Alert(
                type="site_blocked" if status == "blocked" else "site_down",
                site_id=site.id,
                payload={
                    "url": capture.url,
                    "http_status": capture.status_code,
                    "error": capture.error,
                },
            )
        )
        return report

    emit_progress(on_progress, "store", "Saving accounts and evidence")
    result = ExtractionResult()
    merged: dict[tuple[str, str, str], object] = {}
    # A run only proves an account is gone if every probe that could have shown it ran.
    complete = True
    # Why it did not, kept apart by whose problem it is. A method the site switched off is
    # not a defect in this collector and nothing here can fix it; a selector that no longer
    # matches is ours to repair. Reporting both as "partial" told an operator to go and fix
    # something that was never broken, so the two are tracked separately and only the
    # second one degrades the run.
    declined: list[str] = []
    broken: list[str] = []

    for probe, probe_config, probe_capture in captures:
        if probe_capture.unavailable:
            complete = False
            declined.append(probe.name)
            session.add(
                Alert(
                    type="method_unavailable",
                    site_id=site.id,
                    payload={
                        "probe": probe.name,
                        "url": probe_capture.url,
                        "marker": probe_capture.unavailable,
                    },
                )
            )
            continue

        if _capture_status(probe_capture) != "ok":
            complete = False
            broken.append(probe.name)
            session.add(
                Alert(
                    type="probe_failed",
                    site_id=site.id,
                    payload={
                        "probe": probe.name,
                        "url": probe_capture.url,
                        "error": probe_capture.error,
                    },
                )
            )
            continue

        # A broken click flow leaves us on the wrong page, where the selectors legitimately
        # match nothing. Without its own alert that is indistinguishable from a site that
        # simply published no accounts today.
        if probe_capture.flow_error:
            complete = False
            broken.append(probe.name)
            session.add(
                Alert(
                    type="flow_broken",
                    site_id=site.id,
                    payload={
                        "probe": probe.name,
                        "url": probe_capture.url,
                        "error": probe_capture.flow_error,
                    },
                )
            )

        # Evidence is stored before extraction, so a page that later turns out to break the
        # selectors can still be re-processed from the exact bytes we saw. A dry run must
        # not touch the evidence store either, so this is skipped there.
        if not dry_run:
            for blob in store_capture(f"{config.slug}/{_evidence_name(probe.name)}", probe_capture):
                session.add(
                    Evidence(
                        run_id=run.id,
                        kind=blob.kind,
                        path=blob.path,
                        sha256=blob.sha256,
                        bytes=blob.bytes,
                    )
                )
                report.evidence_paths.append(blob.path)

        if probe.merchant:
            name = _merchant_name(probe_capture.text, probe.merchant)
            if name:
                session.add(
                    MerchantSighting(
                        site_id=site.id,
                        run_id=run.id,
                        probe=probe.name,
                        channel=probe.channel or (probe.blocks[0].channel if probe.blocks else ""),
                        merchant_name=name,
                        page_url=probe_capture.url,
                    )
                )
                report.merchants.append(name)

        probe_result = extract(probe_capture.text, probe_config)
        result.selector_hits += probe_result.selector_hits
        result.sweep_hits += probe_result.sweep_hits
        for account in probe_result.accounts:
            merged.setdefault(account.dedup_key, account)

    result.accounts = list(merged.values())
    report.extraction = result
    # Every payee this run brought back, which is what the portal calls it and what the
    # run's own filtered list shows. Two corrections in one line: it counts de-duplicated
    # payees rather than extraction hits, and it counts the name-only ones. A run that
    # collected a Nagad merchant and nothing else was reporting that it had found nothing.
    run.candidates_found = len(result.accounts) + len(set(report.merchants))

    if result.extractor_looks_broken:
        run.status = "partial"
        report.status = "partial"
        session.add(
            Alert(
                type="extractor_broken",
                site_id=site.id,
                payload={
                    "url": capture.url,
                    "sweep_hits": result.sweep_hits,
                    "message": "selectors matched nothing but the page still contains numbers",
                },
            )
        )

    if dry_run:
        # Nothing from this run is kept — not the run row, not the site sync.
        session.rollback()
        report.run_id = None
        return report

    seen_ids, new_ids = record_observations(
        session, run, site.id, result.accounts, page_url=capture.url, seen_at=utcnow()
    )
    report.changes = compute_changeset(
        session, run, site.id, seen_ids, new_ids, complete=complete
    )
    # Only our own breakage degrades the run. A green "ok" beside an empty result is how a
    # broken collector goes unnoticed for a week - but so is a permanent "partial" nobody
    # can clear, which teaches the same operator to ignore the colour entirely.
    if broken:
        run.status = "partial"
        report.status = "partial"

    # Say why, on the run, in the words the reader needs. The alerts carry the detail; this
    # is the one line the runs table and the finished card can show without opening
    # anything, and without it a partial run is a colour with no explanation.
    reasons = []
    if broken:
        reasons.append(
            f"{len(broken)} method{'s' if len(broken) > 1 else ''} could not be read "
            f"({', '.join(sorted(broken))}) - the config may be stale"
        )
    if declined:
        reasons.append(
            f"{len(declined)} method{'s' if len(declined) > 1 else ''} switched off by the "
            f"site ({', '.join(sorted(declined))})"
        )
    if reasons and not run.error:
        run.error = "; ".join(reasons)
        report.error = run.error

    run.accounts_new = len(new_ids)

    affected = sorted(set(seen_ids) | set(report.changes.disappeared_account_ids))
    refresh_active_flags(session, affected)

    for account_id in new_ids:
        session.add(Alert(type="new_account", site_id=site.id, payload={"account_id": account_id}))

    if source_url is not None:
        working = session.scalar(
            select(SiteUrl).where(SiteUrl.site_id == site.id, SiteUrl.url == source_url)
        )
        if working is not None:
            working.last_ok_at = utcnow()

    run.finished_at = utcnow()
    session.flush()
    return report
