"""Assisted login and session auto-recovery.

No real site is contacted: perform_login is exercised only for its guard clauses, and the
run-recovery wiring is driven with stubs so no browser opens.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ght.models import Alert, Base
from ght.sources import Login, SourceConfig


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _login_config():
    return SourceConfig(
        slug="1xbet-bd",
        name="x",
        fetcher="browser",
        logged_out_marker="registration-widget",
        auth_state="data/auth/x.json",
        login=Login(
            url="u", username="#u", password="#p", submit="#s", success=".ok", assisted=True
        ),
    )


def test_perform_login_needs_a_login_block():
    from ght.auth_login import perform_login

    config = SourceConfig(slug="x", name="x", fetcher="browser", auth_state="data/auth/x.json")
    result = perform_login(config)
    assert result.ok is False
    assert result.reason == "config"


def test_perform_login_needs_an_auth_state_path():
    from ght.auth_login import perform_login

    config = SourceConfig(
        slug="x",
        name="x",
        fetcher="browser",
        login=Login(url="u", username="#u", password="#p", submit="#s", success=".ok"),
    )
    result = perform_login(config)
    assert result.ok is False
    assert result.reason == "config"


def test_recover_login_reasons(monkeypatch):
    """The note the run reports is derived from the login result's reason."""
    from ght.auth_login import LoginResult
    from ght.pipeline import run as runmod

    config = _login_config()

    monkeypatch.setattr("ght.auth_login.perform_login", lambda c, *a: LoginResult(True, "ok"))
    ok, note = runmod._try_recover_login(config)
    assert ok is True and "login window" in note

    monkeypatch.setattr("ght.auth_login.perform_login", lambda c, *a: LoginResult(False, "timeout"))
    ok, note = runmod._try_recover_login(config)
    assert ok is False and "not completed in time" in note

    plain = SourceConfig(slug="s", name="s", fetcher="browser")
    ok, note = runmod._try_recover_login(plain)
    assert ok is False and "no login flow" in note


def test_run_site_retries_after_recovering_login(session, monkeypatch):
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    expired = RawCapture(url="https://x/office", status_code=200, html="registration-widget")
    failed = RawCapture(url="https://x/office", status_code=0, error="stop here")

    calls = {"n": 0}

    def collect_stub(config):
        calls["n"] += 1
        if calls["n"] == 1:
            return [("p", "c", expired)], "https://x", True
        return [("p", "c", failed)], "https://x", False  # recovered, then bail out cheaply

    monkeypatch.setattr(runmod, "_collect_captures", collect_stub)
    monkeypatch.setattr(runmod, "_try_recover_login", lambda c: (True, "signed in"))

    runmod.run_site(session, _login_config())
    assert calls["n"] == 2
    assert "auth_refreshed" in {a.type for a in session.scalars(select(Alert))}


def test_run_site_reports_when_recovery_fails(session, monkeypatch):
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    expired = RawCapture(url="https://x/office", status_code=200, html="registration-widget")
    monkeypatch.setattr(
        runmod, "_collect_captures", lambda config: ([("p", "c", expired)], "u", True)
    )
    monkeypatch.setattr(
        runmod, "_try_recover_login", lambda c: (False, "the sign-in window was not completed in time")
    )

    report = runmod.run_site(session, _login_config())
    assert report.status == "failed"
    assert "did not recover" in report.error


def test_logged_out_detected_by_login_redirect():
    """A redirect to the login page means the session is dead, even with no body marker."""
    from ght.pipeline.run import _looks_logged_out
    from ght.types import RawCapture

    config = _login_config().model_copy(update={"logged_out_url": "/user/login"})

    on_login = RawCapture(url="https://bd.1xbet.com/en/user/login?return-url=x", status_code=200, html="<form>")
    assert _looks_logged_out(config, on_login) is True

    on_deposit = RawCapture(url="https://bd.1xbet.com/paysystems/deposit/?x", status_code=200, html="<div>")
    assert _looks_logged_out(config, on_deposit) is False


def test_logged_out_detected_by_body_marker():
    from ght.pipeline.run import _looks_logged_out
    from ght.types import RawCapture

    config = _login_config()  # marker "registration-widget"
    hit = RawCapture(url="https://x/", status_code=200, html="...registration-widget...")
    assert _looks_logged_out(config, hit) is True
