"""Orchestration: one collection run against one site."""

from __future__ import annotations

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
from ght.sources import Probe, SourceConfig
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
    }
    if config.timeout is not None:
        kwargs["timeout"] = config.timeout
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
    """
    if config.probes:
        return config.probes
    return [
        Probe(name=config.slug, flow=config.flow, wait_for=config.wait_for, blocks=config.blocks)
    ]


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


def run_site(session: Session, config: SourceConfig, dry_run: bool = False) -> RunReport:
    """Fetch, extract, and persist one site's deposit accounts."""
    site = sync_site(session, config)

    captures = []
    source_url: str | None = None
    for probe in probes_of(config):
        probe_config = config_for_probe(config, probe)
        probe_capture, probe_url = fetch_first_working_url(probe_config)
        captures.append((probe, probe_config, probe_capture))
        if source_url is None:
            # Which configured URL answered, recorded once from the first probe.
            source_url = probe_url

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
        started_at=utcnow(),
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

    if status != "ok":
        run.finished_at = utcnow()
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

    result = ExtractionResult()
    merged: dict[tuple[str, str, str], object] = {}
    # A run only proves an account is gone if every probe that could have shown it ran.
    complete = True

    for probe, probe_config, probe_capture in captures:
        if _capture_status(probe_capture) != "ok":
            complete = False
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
            for blob in store_capture(f"{config.slug}/{probe.name}", probe_capture):
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
    run.candidates_found = result.selector_hits + result.sweep_hits

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
        session, run, site.id, result.accounts, page_url=capture.url
    )
    report.changes = compute_changeset(
        session, run, site.id, seen_ids, new_ids, complete=complete
    )
    if not complete:
        # Say so on the run itself: a green "ok" beside an empty result is how a broken
        # collector goes unnoticed for a week.
        run.status = "partial"
        report.status = "partial"

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
