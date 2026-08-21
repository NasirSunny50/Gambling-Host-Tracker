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

from ght.api.routes import templates

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
    html = render(
        "dashboard.html",
        totals={"accounts": 25, "active": 12, "review": 3, "sites": 2},
        by_channel=[("bkash", 8), ("nagad", 4)],
        runs=[run_row()],
        newest=[account()],
        sites=SITES,
    )
    assert "Active accounts" in html
    assert ">12<" in html


def test_overview_before_anything_is_collected_offers_a_first_run():
    html = render(
        "dashboard.html",
        totals={"accounts": 0, "active": 0, "review": 0, "sites": 0},
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
    assert "name-row" in html  # the tint is what marks a name-only row now
    # The list answers "where did this come from" rather than repeating a status.
    assert "demo-site" in html


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


RUNS_BASE = {
    "rows": [run_row()],
    "sites": SITES,
    "evidence_counts": {7: 16},
    "runnable": [{"slug": "demo-site", "name": "Demo Site", "status": "active", "order_probes": []}],
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
