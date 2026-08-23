"""End-to-end pipeline test.

The fixture page is served over a real local HTTP server, so the HTTP fetcher, evidence
store, extractor, de-duplication and changeset all run exactly as they do in production.
Nothing here touches the internet.
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ght.config import settings
from ght.models import (
    Account,
    AccountSite,
    Alert,
    Base,
    CollectionRun,
    Evidence,
    Observation,
    Site,
    utcnow,
)
from ght.pipeline.changeset import compute_changeset
from ght.pipeline.evidence import verify
from ght.pipeline.run import run_site
from ght.sources import Block, SourceConfig, SourceUrl

FIXTURES = Path(__file__).parent / "fixtures" / "html"

BLOCKS = [
    Block(channel="bkash", container=".payment-method.bkash", value=".account-number"),
    Block(channel="nagad", container=".payment-method.nagad", value=".account-number"),
    Block(channel="rocket", container=".payment-method.rocket", value=".account-number"),
    Block(channel="bank_transfer", container=".bank-details", value=".acc-no"),
]


@pytest.fixture(scope="module")
def server():
    """Serve tests/fixtures/html on a random localhost port."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A throwaway SQLite database and evidence directory per test."""
    monkeypatch.setattr(settings, "evidence_dir", tmp_path / "evidence")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as active:
        yield active


def make_config(base_url: str, page: str = "demo_site_deposit.html", **overrides) -> SourceConfig:
    data = {
        "slug": "demo-site",
        "name": "Demo Betting (fixture)",
        "fetcher": "http",
        "urls": [SourceUrl(url=f"{base_url}/{page}")],
        "blocks": BLOCKS,
        "ignore_numbers": ["01500000000"],
    }
    data.update(overrides)
    return SourceConfig(**data)


def test_full_run_persists_accounts_observations_and_evidence(session, server, tmp_path):
    report = run_site(session, make_config(server))
    session.commit()

    assert report.status == "ok"
    assert report.http_status == 200

    accounts = list(session.scalars(select(Account)))
    assert len(accounts) == 4
    assert {a.channel for a in accounts} == {"bkash", "nagad", "rocket", "bank_transfer"}
    assert all(a.is_active for a in accounts)
    assert all(not a.needs_review for a in accounts)

    assert session.query(Observation).count() == 4
    assert session.query(AccountSite).count() == 4

    run = session.scalar(select(CollectionRun))
    assert run.status == "ok"
    assert run.accounts_new == 4
    assert run.finished_at is not None


def test_evidence_blob_is_stored_and_rehashes_to_the_recorded_digest(session, server):
    run_site(session, make_config(server))
    session.commit()

    blob = session.scalar(select(Evidence))
    assert blob.kind == "html"
    assert len(blob.sha256) == 64
    assert blob.bytes > 0
    # This is the property the whole evidence design exists for.
    assert verify(blob.path, blob.sha256) is True


def test_second_run_of_an_unchanged_page_adds_observations_not_accounts(session, server):
    config = make_config(server)
    run_site(session, config)
    session.commit()
    second = run_site(session, config)
    session.commit()

    assert session.query(Account).count() == 4
    assert session.query(Observation).count() == 8
    assert second.changes.new_account_ids == []
    assert second.changes.disappeared_account_ids == []

    account = session.scalar(select(Account).where(Account.channel == "bkash"))
    assert account.observation_count == 2
    assert account.first_seen_at < account.last_seen_at


def test_rotated_numbers_produce_new_accounts_and_flag_the_old_ones_gone(session, server):
    run_site(session, make_config(server))
    session.commit()

    # Same site, next collection: the operator has rotated the wallet numbers.
    rotated = run_site(session, make_config(server, page="demo_site_deposit_rotated.html"))
    session.commit()

    assert len(rotated.changes.new_account_ids) == 3
    assert len(rotated.changes.disappeared_account_ids) == 3

    # Nothing is overwritten - both generations are on file.
    assert session.query(Account).count() == 7
    assert session.query(Observation).count() == 8

    old = session.scalar(select(Account).where(Account.account_number == "+8801712345678"))
    assert old is not None
    # Still blocklisted: it was collecting deposits hours ago.
    assert old.is_active is True

    # The bank account did not change, so it is not reported as new or gone.
    bank = session.scalar(select(Account).where(Account.channel == "bank_transfer"))
    assert bank.observation_count == 2


def test_one_account_used_by_two_sites_is_linked_to_both(session, server):
    run_site(session, make_config(server))
    session.commit()
    run_site(session, make_config(server, slug="demo-site-2", name="Second brand"))
    session.commit()

    # The shared-operator signal: same wallets, two brands, one account row each.
    assert session.query(Account).count() == 4
    assert session.query(AccountSite).count() == 8

    account = session.scalar(select(Account).where(Account.channel == "bkash"))
    assert len({link.site_id for link in account.site_links}) == 2


def test_new_accounts_raise_alerts(session, server):
    run_site(session, make_config(server))
    session.commit()

    alerts = list(session.scalars(select(Alert).where(Alert.type == "new_account")))
    assert len(alerts) == 4


def test_unreachable_site_is_recorded_as_failed_and_alerts(session, server):
    report = run_site(session, make_config(server, page="does-not-exist.html"))
    session.commit()

    assert report.status == "failed"
    assert session.query(Account).count() == 0

    run = session.scalar(select(CollectionRun))
    assert run.status == "failed"
    assert run.http_status == 404

    alert = session.scalar(select(Alert).where(Alert.type == "site_down"))
    assert alert is not None


def test_stale_selectors_mark_the_run_partial_and_alert(session, server):
    broken = [block.model_copy(update={"container": ".gone"}) for block in BLOCKS]
    report = run_site(session, make_config(server, blocks=broken))
    session.commit()

    assert report.status == "partial"
    assert session.scalar(select(Alert).where(Alert.type == "extractor_broken")) is not None

    # The sweep still recovered the numbers, but nothing vouches for them.
    assert session.query(Account).count() == 4
    assert all(a.needs_review for a in session.scalars(select(Account)))


def test_dry_run_writes_nothing(session, server, tmp_path):
    report = run_site(session, make_config(server), dry_run=True)

    assert report.account_count == 4
    assert report.run_id is None
    assert session.query(Account).count() == 0
    assert session.query(CollectionRun).count() == 0
    assert session.query(Evidence).count() == 0
    assert not (tmp_path / "evidence").exists()


def test_absence_is_only_concluded_from_a_complete_run(session, server):
    """An expired login makes every account look gone; absence needs a full sweep."""
    run_site(session, make_config(server), dry_run=False)
    session.commit()

    site = session.scalar(select(Site).where(Site.slug == "demo-site"))
    later = CollectionRun(site_id=site.id, status="ok", fetcher="http", started_at=utcnow())
    session.add(later)
    session.flush()

    # This run saw nothing at all. Whether that means "gone" depends entirely on whether
    # it managed to look everywhere it was supposed to.
    partial = compute_changeset(session, later, site.id, [], [], complete=False)
    assert partial.disappeared_account_ids == []

    complete = compute_changeset(session, later, site.id, [], [], complete=True)
    assert len(complete.disappeared_account_ids) == 4


# ------------------------------------- whose fault an incomplete run is


def _probe_config(server, probes):
    # A probed source carries no top-level blocks: each probe brings its own.
    return make_config(server, probes=probes, blocks=[], fetcher="browser", frame="/deposit")


def _captures(monkeypatch, per_probe, page="demo_site_deposit.html"):
    """Answer each probe with a prepared capture, keyed by probe name."""
    from datetime import UTC, datetime

    import ght.pipeline.run as run_module
    from ght.types import RawCapture

    html = (FIXTURES / page).read_text(encoding="utf-8")

    def fake_fetch(config):
        name = config.wait_for  # the per-probe copy carries its own wait_for; used as a key
        kind = per_probe[name]
        capture = RawCapture(
            url="https://demo.invalid/deposit",
            status_code=200,
            html=html,
            fetcher="browser",
            fetched_at=datetime.now(UTC),
            flow_error="flow step 1 ('.gone') failed: TimeoutError" if kind == "broken" else None,
            unavailable=".modal-payment--method-undefined" if kind == "declined" else None,
        )
        return capture, config.urls[0].url

    monkeypatch.setattr(run_module, "fetch_first_working_url", fake_fetch)


def test_a_method_the_site_switched_off_does_not_degrade_the_run(session, server, monkeypatch):
    """The operator turned it off. Nothing here is broken and nothing here can fix it, so
    calling the run "partial" sends someone to look for a fault that does not exist."""
    from ght.sources import Probe

    probes = [
        Probe(name="works", wait_for="#works", blocks=BLOCKS),
        Probe(name="switched-off", wait_for="#off", blocks=BLOCKS),
    ]
    _captures(monkeypatch, {"#works": "ok", "#off": "declined"})
    report = run_site(session, _probe_config(server, probes))
    session.commit()

    assert report.status == "ok"
    assert session.scalar(select(Alert).where(Alert.type == "method_unavailable")) is not None
    # It still says so, in the words the runs table shows.
    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert "switched off by the site" in run.error
    assert "switched-off" in run.error


def test_a_broken_selector_still_makes_the_run_partial(session, server, monkeypatch):
    from ght.sources import Probe

    probes = [
        Probe(name="works", wait_for="#works", blocks=BLOCKS),
        Probe(name="stale", wait_for="#stale", blocks=BLOCKS),
    ]
    _captures(monkeypatch, {"#works": "ok", "#stale": "broken"})
    report = run_site(session, _probe_config(server, probes))
    session.commit()

    assert report.status == "partial"
    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert "config may be stale" in run.error
    assert "stale" in run.error


def test_both_kinds_are_reported_separately_in_one_run(session, server, monkeypatch):
    from ght.sources import Probe

    probes = [
        Probe(name="works", wait_for="#works", blocks=BLOCKS),
        Probe(name="switched-off", wait_for="#off", blocks=BLOCKS),
        Probe(name="stale", wait_for="#stale", blocks=BLOCKS),
    ]
    _captures(monkeypatch, {"#works": "ok", "#off": "declined", "#stale": "broken"})
    run_site(session, _probe_config(server, probes))
    session.commit()

    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert "config may be stale" in run.error and "switched off by the site" in run.error
    assert run.status == "partial"  # because of the stale one, not the switched-off one


def test_an_incomplete_run_never_concludes_an_account_is_gone(session, server, monkeypatch):
    """Blame and evidence are different questions. A run can be healthy and still have
    seen too little to prove anything disappeared."""
    from ght.sources import Probe

    probes = [
        Probe(name="works", wait_for="#works", blocks=BLOCKS),
        Probe(name="switched-off", wait_for="#off", blocks=BLOCKS),
    ]
    _captures(monkeypatch, {"#works": "ok", "#off": "declined"})
    report = run_site(session, _probe_config(server, probes))
    session.commit()

    assert report.status == "ok"
    assert report.changes.disappeared_account_ids == []


def test_a_name_only_payee_counts_as_found(session, server, monkeypatch):
    """A run that collected a Nagad merchant and no numbered account was reporting that it
    found nothing - while its own filtered payee list showed the merchant."""
    from ght.sources import Probe

    probes = [Probe(name="named", wait_for="#works", channel="nagad", merchant=".merchant-name")]
    _captures(monkeypatch, {"#works": "ok"}, page="psp_merchant_page.html")
    config = make_config(server, probes=probes, blocks=[], fetcher="browser", frame="/deposit")

    report = run_site(session, config)
    session.commit()

    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert report.merchants, "the fixture page should name a merchant"
    assert run.candidates_found == len(set(report.merchants))


def test_found_counts_payees_rather_than_extraction_hits(session, server):
    """The figure is labelled "payees found" and sits beside a list of them, so it has to
    be the size of that list - not how many times the extractors matched something."""
    report = run_site(session, make_config(server))
    session.commit()

    run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
    assert run.candidates_found == report.account_count + len(set(report.merchants))

# --------------------------------------------------- which screenshot belongs to a payee


def _store(tmp_path, probe, name, body, shot=b"PNG"):
    """Write one probe's evidence the way a fetch would, and return its two rows."""
    from ght.models import Evidence
    from ght.pipeline.evidence import store_blob

    html = store_blob(f"melbet-bd/{probe}", "html", body.encode("utf-8"), root=tmp_path)
    png = store_blob(f"melbet-bd/{probe}", "screenshot", shot + probe.encode(), root=tmp_path)
    return (
        Evidence(run_id=1, kind="html", path=html.path, sha256=html.sha256, bytes=html.bytes),
        Evidence(run_id=1, kind="screenshot", path=png.path, sha256=png.sha256, bytes=png.bytes),
    )


def _payee_fixture(session, tmp_path, monkeypatch):
    """One fetch of three methods, each publishing its own wallet."""
    from ght.api.routes import settings as route_settings
    from ght.models import Account, CollectionRun, Observation, Site

    monkeypatch.setattr(route_settings, "evidence_dir", tmp_path)

    site = Site(slug="melbet-bd", name="Melbet")
    session.add(site)
    session.flush()
    run = CollectionRun(id=1, site_id=site.id, status="ok", fetcher="browser", started_at=utcnow())
    session.add(run)
    session.flush()

    pages = {
        "cellfin-free": "<html><body><p>CellFin Free Wallet Number 01351752316</p></body></html>",
        "rocket": "<html><body><p>Rocket wallet 016287960189</p></body></html>",
        "upay": "<html><body><p>Upay number 01853678501</p></body></html>",
    }
    for probe, body in pages.items():
        for row in _store(tmp_path, probe, probe, body):
            session.add(row)

    accounts = {}
    for channel, number in (
        ("cellfin", "+8801351752316"),
        ("rocket", "+8801628796018"),
        ("upay", "+8801853678501"),
    ):
        account = Account(channel=channel, account_number=number, bank_key="")
        session.add(account)
        session.flush()
        session.add(
            Observation(
                run_id=run.id, site_id=site.id, account_id=account.id,
                raw_text=number, origin="selector", confidence=0.9, observed_at=utcnow(),
            )
        )
        accounts[channel] = account
    session.flush()
    return accounts


def test_a_payee_gets_the_picture_of_its_own_method(session, tmp_path, monkeypatch):
    """The bug this covers put one payee's evidence on another payee's page. Stored numbers
    are canonical (+8801...) and pages print the national form, so matching the stored
    string against the page never hit for a mobile wallet - and every one of them fell back
    to whichever screenshot came first."""
    from ght.api.routes import _probe_of, _screenshot_for

    accounts = _payee_fixture(session, tmp_path, monkeypatch)

    assert _probe_of(_screenshot_for(session, accounts["cellfin"])) == "cellfin-free"
    assert _probe_of(_screenshot_for(session, accounts["rocket"])) == "rocket"
    assert _probe_of(_screenshot_for(session, accounts["upay"])) == "upay"


def test_a_rocket_wallet_matches_the_check_digit_the_page_prints(session, tmp_path, monkeypatch):
    """Rocket publishes twelve digits and the account is keyed on the eleven that identify
    it, so the page never contains the stored string exactly."""
    from ght.api.routes import _probe_of, _screenshot_for

    accounts = _payee_fixture(session, tmp_path, monkeypatch)
    assert _probe_of(_screenshot_for(session, accounts["rocket"])) == "rocket"


def test_no_picture_beats_the_wrong_picture(session, tmp_path, monkeypatch):
    """When no stored page can be shown to have published the number, the page says so.
    Showing some other method's screenshot would be evidence of the wrong thing."""
    from ght.api.routes import _screenshot_for
    from ght.models import Account, Observation

    accounts = _payee_fixture(session, tmp_path, monkeypatch)
    site_id = session.scalar(select(Site.id))

    stranger = Account(channel="bkash", account_number="+8801999999999", bank_key="")
    session.add(stranger)
    session.flush()
    session.add(
        Observation(
            run_id=1, site_id=site_id, account_id=stranger.id, raw_text="+8801999999999",
            origin="regex_sweep", confidence=0.3, observed_at=utcnow(),
        )
    )
    session.flush()

    assert _screenshot_for(session, stranger) is None
    # And the payees that are on those pages are unaffected.
    assert _screenshot_for(session, accounts["upay"]) is not None
