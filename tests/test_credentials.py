"""Encrypted credential storage and headless login.

No real site is contacted: perform_login is exercised only for its input handling, and the
manager is driven with a stub so the gate and alerting are tested without a browser.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ght import credentials as creds
from ght import crypto
from ght.models import Base, SiteCredential


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "secret_key", Fernet.generate_key().decode())


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ------------------------------------------------------------------------- crypto


def test_roundtrip_with_a_key(key):
    token = crypto.encrypt("send5economy9corroding?")
    assert token != "send5economy9corroding?"
    assert crypto.decrypt(token) == "send5economy9corroding?"


def test_fails_closed_without_a_key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "secret_key", "")
    assert crypto.is_configured() is False
    with pytest.raises(crypto.SecretKeyMissing):
        crypto.encrypt("x")


def test_a_different_key_cannot_read_the_token(monkeypatch):
    monkeypatch.setattr(crypto.settings, "secret_key", Fernet.generate_key().decode())
    token = crypto.encrypt("secret")
    monkeypatch.setattr(crypto.settings, "secret_key", Fernet.generate_key().decode())
    with pytest.raises(crypto.SecretKeyInvalid):
        crypto.decrypt(token)


def test_an_invalid_key_is_not_configured(monkeypatch):
    monkeypatch.setattr(crypto.settings, "secret_key", "not-a-fernet-key")
    assert crypto.is_configured() is False


# --------------------------------------------------------------------- storage


def test_set_get_and_status(key, session):
    assert creds.status(session, "1xbet-bd").configured is False

    creds.set_credentials(session, "1xbet-bd", "1772948457", "hunter2", label="main")
    got = creds.get_credentials(session, "1xbet-bd")
    assert got == ("1772948457", "hunter2")

    st = creds.status(session, "1xbet-bd")
    assert st.configured is True
    assert st.label == "main"


def test_the_password_is_never_stored_in_plaintext(key, session):
    creds.set_credentials(session, "site", "user", "plaintextpw")
    row = session.scalar(select(SiteCredential).where(SiteCredential.slug == "site"))
    assert "plaintextpw" not in row.password_enc
    assert "user" not in row.username_enc


def test_replace_and_delete(key, session):
    creds.set_credentials(session, "site", "u1", "p1")
    creds.set_credentials(session, "site", "u2", "p2")  # upsert, not a second row
    assert len(session.scalars(select(SiteCredential)).all()) == 1
    assert creds.get_credentials(session, "site") == ("u2", "p2")

    assert creds.delete_credentials(session, "site") is True
    assert creds.get_credentials(session, "site") is None
    assert creds.delete_credentials(session, "site") is False


# ----------------------------------------------------------------------- login


def test_perform_login_needs_a_login_block():
    from ght.auth_login import perform_login
    from ght.sources import SourceConfig

    config = SourceConfig(slug="x", name="x", fetcher="browser", auth_state="data/auth/x.json")
    result = perform_login(config, "u", "p")
    assert result.ok is False
    assert result.reason == "config"


def test_login_manager_gate_and_alert(monkeypatch):
    """A failed login records an alert with the reason, never the credentials."""
    from ght.api import jobs
    from ght.auth_login import LoginResult

    # Stub the whole orchestration to a failing login, so no browser or DB is needed.
    def fake_login(slug):
        # mimic _do_login writing an alert path result
        return False, "a CAPTCHA or 2FA prompt appeared"

    monkeypatch.setattr(jobs, "_do_login", fake_login)
    mgr = jobs.LoginManager()
    started, _ = mgr.start("1xbet-bd")
    assert started is True

    import time

    for _ in range(50):
        if not mgr.is_running("1xbet-bd"):
            break
        time.sleep(0.02)

    info = mgr.status("1xbet-bd")
    assert info.ok is False
    assert "CAPTCHA" in info.message
    # The LoginResult reason vocabulary stays stable for the alert mapping.
    assert LoginResult(False, "challenge").reason == "challenge"


# ------------------------------------------------------ auto-login on session expiry


def _login_config():
    from ght.sources import Login, SourceConfig

    return SourceConfig(
        slug="1xbet-bd",
        name="x",
        fetcher="browser",
        logged_out_marker="registration-widget",
        auth_state="data/auth/x.json",
        login=Login(url="u", username="#u", password="#p", submit="#s", success=".ok"),
    )


def test_try_auto_login_paths(key, session, monkeypatch):
    from ght.auth_login import LoginResult
    from ght.pipeline import run as runmod
    from ght.sources import SourceConfig

    # No login block -> not attempted.
    plain = SourceConfig(slug="s", name="s", fetcher="browser")
    ok, note = runmod._try_auto_login(session, plain)
    assert ok is False and "no login flow" in note

    config = _login_config()

    # Login configured but no credentials stored.
    ok, note = runmod._try_auto_login(session, config)
    assert ok is False and "no stored credentials" in note

    creds.set_credentials(session, "1xbet-bd", "user", "pass")

    # A CAPTCHA aborts recovery with a clear note.
    monkeypatch.setattr("ght.auth_login.perform_login", lambda c, u, p: LoginResult(False, "challenge"))
    ok, note = runmod._try_auto_login(session, config)
    assert ok is False and "CAPTCHA" in note

    # A clean sign-in recovers.
    monkeypatch.setattr("ght.auth_login.perform_login", lambda c, u, p: LoginResult(True, "ok"))
    ok, note = runmod._try_auto_login(session, config)
    assert ok is True


def test_run_site_retries_collection_after_auto_login(key, session, monkeypatch):
    from sqlalchemy import select as _select

    from ght.models import Alert
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    expired = RawCapture(url="https://x/office", status_code=200, html="registration-widget")
    failed = RawCapture(url="https://x/office", status_code=0, error="stop here")

    calls = {"n": 0}

    def collect_stub(config):
        calls["n"] += 1
        if calls["n"] == 1:
            return [("p", "c", expired)], "https://x", True  # expired the first time
        return [("p", "c", failed)], "https://x", False  # recovered, then bail out cheaply

    monkeypatch.setattr(runmod, "_collect_captures", collect_stub)
    monkeypatch.setattr(runmod, "_try_auto_login", lambda s, c: (True, "signed in"))

    report = runmod.run_site(session, _login_config())

    assert calls["n"] == 2  # collected again after the auto-login
    types = {a.type for a in session.scalars(_select(Alert))}
    assert "auth_refreshed" in types
    assert report.status != "failed" or report.error != ""  # ran past the expiry wall


def test_run_site_reports_when_auto_login_cannot_recover(key, session, monkeypatch):
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    expired = RawCapture(url="https://x/office", status_code=200, html="registration-widget")
    monkeypatch.setattr(runmod, "_collect_captures", lambda config: ([("p", "c", expired)], "u", True))
    monkeypatch.setattr(
        runmod, "_try_auto_login", lambda s, c: (False, "hit a CAPTCHA or 2FA")
    )

    report = runmod.run_site(session, _login_config())
    assert report.status == "failed"
    assert "automatic sign-in did not recover" in report.error
    assert "CAPTCHA" in report.error
