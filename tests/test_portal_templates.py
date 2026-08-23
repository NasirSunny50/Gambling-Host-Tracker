"""Rendering the portal's pages.

The templates carry real logic — which empty state to show, whether a run is working or
waiting for a person, whether a blank cell means "nothing to collect" or "we failed to
collect it". None of that is exercised by the pipeline tests, and a broken template is a
500 on the one page an analyst is looking at.

These render the templates directly with hand-built contexts, so they stay offline and
cover the states that are otherwise only reachable by driving a real collection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ght.api.routes import PAGE_SIZES, Page, templates

NOW = datetime(2026, 8, 20, 18, 26, tzinfo=UTC)


def render(name: str, **context) -> str:
    """Render one page the way a route would, minus the request plumbing."""
    base = {
        "nav": {"review": 3, "health": "collection healthy", "health_tone": "ok", "now": "2026-08-20 18:26"},
        "page": "",
        "auto_refresh": False,
    }
    return templates.env.get_template(name).render({**base, **context})


def account(**kw):
    fields = {
        "id": 1,
        "channel": "bkash",
        "account_number": "+8801700000000",
        "holder_name": "A Holder",
        "bank_name": None,
        "branch": None,
        "operator": None,
        "account_type": None,
        "confidence": 0.95,
        "observation_count": 4,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "is_active": True,
        "needs_review": False,
    }
    return SimpleNamespace(**{**fields, **kw})


def run_row(**kw):
    fields = {
        "id": 7,
        "site_id": 1,
        "status": "ok",
        "candidates_found": 7,
        "accounts_new": 2,
        "error": None,
        "started_at": NOW,
    }
    return SimpleNamespace(**{**fields, **kw})


def payee_row(**kw):
    fields = {
        "id": 1,
        "kind": "account",
        "channel": "bkash",
        "number": "+8801700000000",
        "name": "A Holder",
        "bank": None,
        "confidence": 0.95,
        "times": 4,
        "first_seen": NOW,
        "last_seen": NOW,
        "is_active": True,
        "needs_review": False,
        "site": "demo-site",
    }
    return SimpleNamespace(**{**fields, **kw})


def page(total=1, number=1, size=10):
    from ght.api.routes import Page

    return Page(number=number, size=size, total=total)


SITES = {1: SimpleNamespace(id=1, slug="demo-site", name="Demo Site")}


# ----------------------------------------------------------------------- overview


def test_overview_with_data_shows_the_figures():
    """What has been collected, and how much looking it took. An account count alone says
    nothing about whether the collector has been running."""
    html = render(
        "dashboard.html",
        totals={"accounts": 25, "active": 12, "review": 3, "sites": 2, "runs": 42},
        by_channel=[("bkash", 8), ("nagad", 4)],
        first_run_at=NOW,
        runs=[run_row()],
        newest=[payee_row(), payee_row(id=9, kind="merchant", number=None, name="A Merchant",
                          channel="nagad", bank=None)],
        sites=SITES,
    )
    assert "Accounts found" in html
    assert ">25<" in html
    assert "Collections run" in html
    assert ">42<" in html
    # NOW is 18:26 UTC, which is the 21st in Dhaka - the caption follows the reader's day,
    # not the server's.
    assert "since 21/08/2026" in html
    # A name-only payee is one of the accounts, and it is reachable from here.
    assert "A Merchant" in html
    assert 'href="/merchants/9"' in html


def test_overview_before_anything_is_collected_offers_a_first_run():
    html = render(
        "dashboard.html",
        totals={"accounts": 0, "active": 0, "review": 0, "sites": 0, "runs": 0},
        by_channel=[],
        runs=[],
        newest=[],
        sites={},
    )
    assert "Nothing collected yet" in html
    assert 'href="/runs"' in html


# ----------------------------------------------------------------------- payees


def test_a_name_only_payee_reads_as_not_applicable_not_as_missing():
    """The distinction the whole table hangs on: an em-dash for "there is no number to
    collect", an italic "unknown" for "there was one and we did not get it"."""
    html = render(
        "payees.html",
        rows=[
            payee_row(kind="merchant", number=None, bank=None, confidence=None, name="A Merchant"),
            payee_row(id=2, name=None),
        ],
        pagination=page(total=2),
        channels=["bkash"],
        channel=None,
        status="all",
        q="",
        per=10,
        page_sizes=(10, 25),
    )
    assert "Not applicable" in html  # the title spelling out what the em-dash means
    assert "unknown" in html
    # The list answers "where did this come from" rather than repeating a status.
    assert "demo-site" in html
    # Both kinds lead somewhere: a name-only payee to the page that named it.
    assert 'data-href="/merchants/1"' in html
    assert 'data-href="/accounts/2"' in html


def test_filtered_to_nothing_offers_to_clear_rather_than_looking_empty():
    html = render(
        "payees.html",
        rows=[],
        pagination=page(total=0),
        channels=["bkash"],
        channel="bkash",
        status="all",
        q="nothing",
        per=10,
        page_sizes=(10, 25),
    )
    assert "No payees match these filters" in html
    assert "Clear filters" in html


def test_nothing_collected_yet_is_not_the_same_as_no_matches():
    html = render(
        "payees.html",
        rows=[],
        pagination=page(total=0),
        channels=[],
        channel=None,
        status="all",
        q="",
        per=10,
        page_sizes=(10, 25),
    )
    assert "No payees collected yet" in html


# ----------------------------------------------------------------------- detail


def test_detail_shows_the_number_where_it_was_published():
    """A reviewer who does not read HTML needs to see the number on the site, not a list
    of digests. The digest still travels with the picture, so it stays checkable."""
    shot = SimpleNamespace(id=42, run_id=7, kind="screenshot", path="s/p/ab/abc.png",
                           sha256="a" * 64, bytes=1234, captured_at=NOW)
    html = render(
        "account_detail.html",
        account=account(),
        observations=[],
        sites=[],
        evidence_total=16,
        screenshot=shot,
    )
    assert '/evidence/42.png' in html
    assert "sha256:aaaaaaaaaaaa" in html
    assert "16 pages stored" in html


def test_the_copy_sits_on_the_value_it_copies():
    """The number and the name are both retyped into other systems, and a copy control
    parked elsewhere on the page does not read as belonging to either of them."""
    html = render(
        "account_detail.html",
        account=account(),
        observations=[],
        sites=[],
        evidence_total=0,
        screenshot=None,
    )
    number = html.index('data-copy="+8801700000000"')
    holder = html.index('data-copy="A Holder"')
    # Both copies come before the key/value list ends - i.e. they are on the identity
    # block and the holder row, not a button below the panel.
    assert number < html.index("Holder name") < holder < html.index("First seen")
    assert "Copy account number" in html


def test_a_sighting_reports_the_payee_not_the_collector():
    """Origin names a CSS selector. That answers a question about how collection works,
    which is not the question anyone opens a payee to ask."""
    observation = SimpleNamespace(
        id=1, run_id=7, raw_text="01700000000", origin=".payment_modal_row >> .value",
        observed_at=NOW,
    )
    html = render(
        "account_detail.html",
        account=account(),
        observations=[observation],
        sites=[],
        evidence_total=0,
        screenshot=None,
    )
    assert "Sightings" in html
    assert "#7" in html
    assert "Origin" not in html
    assert ".payment_modal_row" not in html


def test_detail_says_so_plainly_when_no_picture_was_captured():
    html = render(
        "account_detail.html",
        account=account(),
        observations=[],
        sites=[],
        evidence_total=0,
        screenshot=None,
    )
    assert "No screenshot was captured" in html


# ----------------------------------------------------------------------- runs


def phases(states):
    names = [("signin", "Sign in to the site"), ("collect", "Read each payment method"),
             ("store", "Save accounts and evidence")]
    return [{"name": n, "label": label, "state": s} for (n, label), s in zip(names, states)]


def job(running=False, current=None):
    """Stand in for the run manager the base layout and the runs page read from."""
    return SimpleNamespace(is_running=running, current=current)


def in_flight(**kw):
    fields = {"slug": "demo-site", "started_at": NOW, "finished_at": None, "returncode": None,
              "message": "Reading nagad", "step": 2, "total": 8, "failed": False}
    return SimpleNamespace(**{**fields, **kw})


def schedule(**kw):
    fields = {"enabled": False, "slug": "", "minutes": 0, "next_due": None,
              "last_started": None, "last_note": ""}
    return SimpleNamespace(**{**fields, **kw})


RUNS_BASE = {
    "p": Page(number=1, size=10, total=1),
    "per": 10,
    "page_sizes": PAGE_SIZES,
    "schedule": schedule(),
    "schedule_seconds": None,
    "schedule_per_day": 0,
    "schedule_min_minutes": 5,
    "rows": [run_row()],
    "sites": SITES,
    "evidence_counts": {7: 16},
    "runnable": [{"slug": "demo-site", "name": "Demo Site", "status": "active"}],
    "job_log": [],
    "last_run": None,
    "elapsed": "1m 30s",
}


def test_idle_runs_page_offers_to_start_one():
    html = render("runs.html", job=job(), job_running=False, waiting=False, seconds_left=None,
                  phases=[], **RUNS_BASE)
    assert "Start a collection run" in html
    assert "Run history" in html


def test_waiting_for_sign_in_says_it_is_not_an_error():
    """The state most likely to be misread as a failure or a hang. It has to name itself."""
    html = render("runs.html", job=job(True, in_flight()), job_running=True, waiting=True,
                  seconds_left=278, phases=phases(["waiting", "pending", "pending"]), **RUNS_BASE)
    assert "not an error" in html
    assert "4:38" in html  # the countdown, from 278 seconds
    assert "browser window has opened" in html
    assert "waiting for you" in html  # the phase state, not spinning as though busy


def test_a_running_collection_shows_the_checklist_not_a_spinner():
    html = render("runs.html", job=job(True, in_flight()), job_running=True, waiting=False,
                  seconds_left=None, phases=phases(["done", "active", "pending"]), **RUNS_BASE)
    assert "Run in progress" in html
    assert "in progress" in html
    assert "refreshing every 5s" in html


def test_a_finished_run_summarises_what_it_collected():
    html = render(
        "runs.html",
        finished=in_flight(finished_at=NOW, returncode=0, message="Finished"),
        job_running=False,
        waiting=False,
        seconds_left=None,
        phases=phases(["done", "done", "done"]),
        **{**RUNS_BASE, "last_run": run_row(status="partial")},
    )
    assert "Run finished" in html
    assert "pages saved" in html
    assert "partial" in html
    # The link after a run asks what *this* run brought in, not what has ever been collected.
    assert 'href="/payees?run=7"' in html


@pytest.mark.parametrize("name", ["components.html"])
def test_the_static_pages_render(name):
    assert "Design notes" in render(name)


def test_a_failed_run_is_not_dressed_up_as_a_finished_one():
    """What the operator saw: three green ticks, a "Run finished" heading and an invitation
    to view payees, on a run whose own error said the session was never valid."""
    stopped = in_flight(finished_at=NOW, returncode=0, failed=True,
                        message="The saved session was not valid")
    html = render(
        "runs.html",
        finished=stopped,
        job_running=False,
        waiting=False,
        seconds_left=None,
        phases=phases(["stopped", "pending", "pending"]),
        **{**RUNS_BASE, "last_run": run_row(status="failed", candidates_found=0, accounts_new=0,
                                            error="Login session expired and sign-in did not recover it")},
    )
    assert "Run stopped" in html
    assert "Run finished" not in html
    assert "/payees?run=" not in html
    assert "Login session expired" in html
    assert "phase--stopped" in html


# ------------------------------------------------ which progress messages mean "your turn"


def test_the_portal_knows_when_a_run_is_waiting_on_a_person():
    """Each of these is a real message the sign-in step emits. Reading any of them as
    ordinary progress would show a spinner while the run stands still waiting for someone."""
    from ght.api.routes import _is_waiting

    waiting = [
        "Waiting for you to sign in - a browser window is open",
        "Still waiting for you to sign in - 240s left",
        "Signed out - opening a browser window for you",
        "The site asked for a CAPTCHA - opening a window for you",
        "Could not sign in unattended (bad_credentials) - opening a window for you",
    ]
    for message in waiting:
        assert _is_waiting(SimpleNamespace(message=message)) is True, message

    working = ["Checking the site sign-in", "Signing in", "Already signed in", "Reading nagad"]
    for message in working:
        assert _is_waiting(SimpleNamespace(message=message)) is False, message


def test_the_outcome_card_stands_down_once_it_has_been_read():
    """It is an announcement, not a state. The load after the run ending carries it; a
    reload gets the page back, and the run is still in the history table below."""
    from ght.api.jobs import RunInfo, RunManager

    manager = RunManager()
    manager._current = RunInfo(slug="demo-site", started_at=NOW, finished_at=NOW, returncode=0)

    assert manager.take_finished() is not None
    assert manager.take_finished() is None


def test_a_run_still_going_is_never_taken_as_finished():
    from ght.api.jobs import RunInfo, RunManager

    manager = RunManager()
    manager._current = RunInfo(slug="demo-site", started_at=NOW)

    assert manager.take_finished() is None


def test_sites_tracked_follows_the_configs_not_the_database(monkeypatch, tmp_path):
    """The sites table is a historical record: a site collected once keeps its rows so its
    runs and evidence still mean something. Counting it reported "2 sites tracked" for a
    fixture nobody tracks any more, so the figure reads the configs on disk instead."""
    from pathlib import Path

    from ght.api.routes import _runnable_sites
    from ght.config import settings

    source = Path("tests/fixtures/demo-site.yaml").read_text(encoding="utf-8")
    (tmp_path / "one.yaml").write_text(source, encoding="utf-8")
    monkeypatch.setattr(settings, "sources_dir", tmp_path)
    assert len(_runnable_sites()) == 1

    (tmp_path / "two.yaml").write_text(source.replace("demo-site", "demo-site-2"), encoding="utf-8")
    assert len(_runnable_sites()) == 2


def test_a_name_only_payee_has_a_page_and_a_picture():
    """It has no number to copy anywhere, so the screenshot of the checkout that named it
    is the whole of the evidence - and until it had a page, nobody could see it."""
    sighting = SimpleNamespace(
        id=9, merchant_name="ALADDIN EXPRESS", channel="nagad", probe="fast-nagad",
        run_id=44, site_id=1, seen_at=NOW,
    )
    shot = SimpleNamespace(id=316, run_id=44, kind="screenshot", sha256="b" * 64,
                           path="1xbet-bd/fast-nagad/57/57.png", captured_at=NOW)
    html = render(
        "merchant_detail.html",
        merchant=sighting,
        sightings=[sighting],
        site=SimpleNamespace(slug="1xbet-bd", name="1xBet"),
        screenshot=shot,
    )
    assert "ALADDIN EXPRESS" in html
    assert "/evidence/316.png" in html
    assert "Not applicable" in html  # there is no account number, and it says so
    assert 'data-copy="ALADDIN EXPRESS"' in html


def test_a_name_only_payee_says_when_no_picture_was_kept():
    sighting = SimpleNamespace(
        id=9, merchant_name="ASHA BEKARY", channel="nagad", probe="fast-nagad",
        run_id=40, site_id=1, seen_at=NOW,
    )
    html = render(
        "merchant_detail.html",
        merchant=sighting,
        sightings=[sighting],
        site=SimpleNamespace(slug="1xbet-bd", name="1xBet"),
        screenshot=None,
    )
    assert "No screenshot was captured" in html


# ------------------------------------------------------------------ the schedule


def test_run_history_is_paged_like_every_other_list():
    """The history is where a number gets a date attached to it, so it has to be walkable
    rather than cut off at whatever the newest screenful happens to be."""
    html = render(
        "runs.html", job=job(), job_running=False, waiting=False, seconds_left=None,
        phases=[], **{**RUNS_BASE, "p": Page(number=2, size=10, total=110)},
    )
    assert "Showing 11" in html and "of 110 runs" in html
    assert 'href="/runs?per=10&amp;page=1"' in html  # Prev keeps the per-page choice
    assert 'href="/runs?per=10&amp;page=3"' in html


def test_the_schedule_offers_intervals_before_it_is_set():
    html = render("runs.html", job=job(), job_running=False, waiting=False, seconds_left=None,
                  phases=[], **RUNS_BASE)
    assert "Run on a schedule" in html
    assert 'data-minutes="60"' in html
    assert 'action="/schedule"' in html


def test_a_live_schedule_says_when_the_next_one_lands():
    """The question someone opens this page to answer is "is it still running, and when
    next" — so the countdown is the headline, not a setting to be read back."""
    html = render(
        "runs.html",
        job=job(), job_running=False, waiting=False, seconds_left=None, phases=[],
        **{**RUNS_BASE,
           "schedule": schedule(enabled=True, slug="demo-site", minutes=30, last_started=NOW),
           "schedule_seconds": 754,
           "schedule_per_day": 48},
    )
    assert "Every 30 minutes" in html
    assert "12:34" in html  # 754 seconds
    assert "48 collections a day" in html
    assert 'action="/schedule/stop"' in html
    # Setting it again is not offered while it is set: stop is the way out.
    assert 'class="sched-form"' not in html


def test_a_skipped_tick_says_why():
    """A schedule that silently does nothing is indistinguishable from one that is off."""
    html = render(
        "runs.html",
        job=job(), job_running=False, waiting=False, seconds_left=None, phases=[],
        **{**RUNS_BASE,
           "schedule": schedule(enabled=True, slug="demo-site", minutes=15,
                                last_note="skipped — the previous collection was still running"),
           "schedule_seconds": 60,
           "schedule_per_day": 96},
    )
    assert "still running" in html


def test_the_schedule_is_visible_while_a_collection_is_in_flight():
    """It is the first thing you look for when a run appears that you did not start."""
    html = render(
        "runs.html",
        job=job(True, in_flight()), job_running=True, waiting=False, seconds_left=None,
        phases=phases(["done", "active", "pending"]),
        **{**RUNS_BASE,
           "schedule": schedule(enabled=True, slug="demo-site", minutes=30),
           "schedule_seconds": 120, "schedule_per_day": 48},
    )
    assert "Run in progress" in html
    assert "Every 30 minutes" in html


def test_the_overview_counts_and_charts_the_same_population():
    """A name-only payee is a payee. Counting it in the figure but not the bars, or in the
    list but not the count, is how the overview came to disagree with itself."""
    from ght.api.routes import _payee_query

    # Both read the same statement, so they cannot drift apart.
    figure = _payee_query(None, None).order_by(None).subquery()
    bars = _payee_query(None, None).order_by(None).subquery()
    assert [c.name for c in figure.columns] == [c.name for c in bars.columns]
    assert "channel" in [c.name for c in figure.columns]
    assert "first_seen" in [c.name for c in figure.columns]
