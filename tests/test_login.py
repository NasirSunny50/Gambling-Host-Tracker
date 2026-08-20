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


def test_sign_in_reasons(monkeypatch):
    """The note the run reports is derived from the login result's reason."""
    from ght.auth_login import LoginResult
    from ght.pipeline import run as runmod

    config = _login_config()

    monkeypatch.setattr(
        "ght.auth_login.perform_login",
        lambda c, *a, **k: LoginResult(True, "ok", "signed in through the login window"),
    )
    ok, note = runmod._sign_in(config)
    assert ok is True and "login window" in note

    monkeypatch.setattr(
        "ght.auth_login.perform_login", lambda c, *a, **k: LoginResult(False, "timeout")
    )
    ok, note = runmod._sign_in(config)
    assert ok is False and "not completed in time" in note

    plain = SourceConfig(slug="s", name="s", fetcher="browser")
    ok, note = runmod._sign_in(plain)
    assert ok is False and "no login flow" in note


def test_run_signs_in_before_collecting(session, monkeypatch):
    """Sign-in happens up front, not after every probe has already failed."""
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    order = []
    capture = RawCapture(url="https://x/office", status_code=0, error="stop here")

    def sign_in_stub(config, on_progress=None):
        order.append("signin")
        return True, "signed in through the login window"

    def collect_stub(config, on_progress=None):
        order.append("collect")
        return [("p", "c", capture)], "https://x", False

    monkeypatch.setattr(runmod, "_sign_in", sign_in_stub)
    monkeypatch.setattr(runmod, "_collect_captures", collect_stub)

    runmod.run_site(session, _login_config())

    assert order == ["signin", "collect"]
    assert "auth_refreshed" in {a.type for a in session.scalars(select(Alert))}


def test_a_dry_run_does_not_open_a_sign_in_window(session, monkeypatch):
    """A dry run must stay side-effect-free, and a login window is very much a side effect."""
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    called = {"signin": False}
    capture = RawCapture(url="https://x/office", status_code=0, error="stop here")

    def sign_in_stub(config, on_progress=None):
        called["signin"] = True
        return True, "signed in"

    monkeypatch.setattr(runmod, "_sign_in", sign_in_stub)
    monkeypatch.setattr(
        runmod, "_collect_captures", lambda c, on_progress=None: ([("p", "c", capture)], "u", False)
    )

    runmod.run_site(session, _login_config(), dry_run=True)
    assert called["signin"] is False


def test_run_reports_when_sign_in_did_not_take(session, monkeypatch):
    """Signing in can appear to work and still leave us logged out; say so plainly."""
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    expired = RawCapture(url="https://x/en/user/login", status_code=200, html="<form>")
    monkeypatch.setattr(
        runmod,
        "_sign_in",
        lambda c, on_progress=None: (False, "the sign-in window was not completed in time"),
    )
    monkeypatch.setattr(
        runmod, "_collect_captures", lambda c, on_progress=None: ([("p", "c", expired)], "u", True)
    )

    report = runmod.run_site(session, _login_config())
    assert report.status == "failed"
    assert "did not recover" in report.error


def test_progress_updates_are_emitted(session, monkeypatch):
    """The portal renders these, so a run must announce each phase it enters."""
    from ght.pipeline import run as runmod
    from ght.types import RawCapture

    capture = RawCapture(url="https://x/office", status_code=0, error="stop here")
    monkeypatch.setattr(runmod, "_sign_in", lambda c, on_progress=None: (True, "signed in"))
    monkeypatch.setattr(
        runmod, "_collect_captures", lambda c, on_progress=None: ([("p", "c", capture)], "u", False)
    )

    seen = []
    runmod.run_site(session, _login_config(), on_progress=seen.append)

    assert "signin" in {u.phase for u in seen}
    assert any("sign-in" in u.message or "signed in" in u.message.lower() for u in seen)


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


def test_saved_session_is_restored_into_the_check_context(tmp_path):
    """The session check is meaningless unless it actually carries the saved cookies."""
    import json

    from ght.auth_login import _restore_context

    state = tmp_path / "auth.json"
    state.write_text(
        json.dumps(
            {
                "storage_state": {"cookies": [{"name": "SESSION"}], "origins": []},
                "user_agent": "Mozilla/5.0 TestAgent",
                "channel": "msedge",
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    class FakeBrowser:
        def new_context(self, **kwargs):
            captured.update(kwargs)
            return "context"

    _restore_context(FakeBrowser(), str(state))
    assert captured["storage_state"]["cookies"] == [{"name": "SESSION"}]
    # The session's own UA must win: Cloudflare ties its clearance cookie to it.
    assert captured["user_agent"] == "Mozilla/5.0 TestAgent"


def test_a_bare_playwright_state_is_also_restored(tmp_path):
    import json

    from ght.auth_login import _restore_context

    state = tmp_path / "auth.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    captured = {}

    class FakeBrowser:
        def new_context(self, **kwargs):
            captured.update(kwargs)
            return "context"

    _restore_context(FakeBrowser(), str(state))
    assert captured["storage_state"] == {"cookies": [], "origins": []}


def test_a_missing_session_file_leaves_the_context_clean(tmp_path):
    from ght.auth_login import _restore_context

    captured = {}

    class FakeBrowser:
        def new_context(self, **kwargs):
            captured.update(kwargs)
            return "context"

    _restore_context(FakeBrowser(), str(tmp_path / "nope.json"))
    assert "storage_state" not in captured
