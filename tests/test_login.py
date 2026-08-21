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


# ----------------------------------------------- is the saved session actually usable


class FakePage:
    """Just enough of a Playwright page for the signed-in check."""

    def __init__(self, url="https://x/account", content="", frames=()):
        self.url = url
        self._content = content
        self.frames = [type("F", (), {"url": u})() for u in frames]
        self.waited_ms = 0

    def content(self):
        return self._content

    def wait_for_timeout(self, ms):
        self.waited_ms += ms


def _framed_config(**kw):
    fields = {
        "slug": "1xbet-bd",
        "name": "x",
        "fetcher": "browser",
        "logged_out_marker": "registration-layout-widget",
        "logged_out_url": "/user/login",
        "frame": "payments.example",
        "auth_state": "data/auth/x.json",
    }
    return SourceConfig(**{**fields, **kw})


def test_a_session_that_never_loads_the_payment_app_counts_as_signed_out():
    """The failure this pins: 1xBet serves the account shell to a dead session — no
    redirect, no logged-out marker — and only refuses when the payment iframe is asked
    for. Judging the shell alone reported "already signed in" and the first probe then
    reported "the site signed us out"."""
    from ght.auth_login import _page_is_signed_in

    page = FakePage(content="<html>account shell</html>", frames=["https://x/other"])
    assert _page_is_signed_in(page, _framed_config(), require_frame=True) is False


def test_a_session_that_loads_the_payment_app_is_signed_in():
    from ght.auth_login import _page_is_signed_in

    page = FakePage(content="<html>ok</html>", frames=["https://payments.example/deposit"])
    assert _page_is_signed_in(page, _framed_config(), require_frame=True) is True


def test_the_logged_out_layout_ends_the_wait_early():
    """It must not sit out the whole frame timeout once the site has said no."""
    from ght.auth_login import FRAME_WAIT_MS, _page_is_signed_in

    page = FakePage(content="<div class=registration-layout-widget>")
    assert _page_is_signed_in(page, _framed_config(), require_frame=True) is False
    assert page.waited_ms < FRAME_WAIT_MS


def test_a_site_without_an_embedded_app_is_judged_on_the_page_alone():
    from ght.auth_login import _page_is_signed_in

    page = FakePage(content="<html>deposit page</html>")
    assert _page_is_signed_in(page, _framed_config(frame=None), require_frame=True) is True


def test_a_redirect_to_the_login_page_still_wins_immediately():
    from ght.auth_login import _page_is_signed_in

    page = FakePage(url="https://x/en/user/login", content="")
    assert _page_is_signed_in(page, _framed_config(), require_frame=True) is False
    assert page.waited_ms == 0


def test_the_assisted_window_does_not_demand_a_deposit_iframe():
    """Where the frame test does not belong. After signing in, the site can land the
    operator on its homepage; insisting on a payment iframe there would reject a sign-in
    that actually worked and leave the window open until it timed out."""
    from ght.auth_login import _page_is_signed_in

    page = FakePage(url="https://x/en/", content="<html>welcome back</html>")
    assert _page_is_signed_in(page, _framed_config()) is True
    assert page.waited_ms == 0


# ------------------------------------------------------- unattended sign-in, then a person


def test_credentials_come_from_the_environment_under_the_slug():
    from ght.credentials import env_names, for_site

    assert env_names("1xbet-bd") == ("GHT_LOGIN_1XBET_BD_USERNAME", "GHT_LOGIN_1XBET_BD_PASSWORD")
    found = for_site(
        "1xbet-bd",
        env={"GHT_LOGIN_1XBET_BD_USERNAME": "u", "GHT_LOGIN_1XBET_BD_PASSWORD": "p"},
    )
    assert (found.username, found.password) == ("u", "p")
    assert bool(found) is True


def test_half_a_credential_pair_is_no_credential():
    """Signing in with a username and no password just burns a failed attempt against a
    site that counts them."""
    from ght.credentials import for_site

    assert bool(for_site("x", env={"GHT_LOGIN_X_USERNAME": "u"})) is False
    assert bool(for_site("x", env={})) is False


def test_credentials_never_render_themselves():
    """These travel through exception handlers and run reports."""
    from ght.credentials import Credentials

    text = repr(Credentials(username="someone", password="hunter2"))
    assert "hunter2" not in text
    assert "someone" not in text


def _record_attempts(monkeypatch, results):
    """Replace the browser attempt with a recorder returning canned results in order."""
    from ght.auth_login import LoginResult

    calls = []

    def fake(config, username, password, timeout_ms, headed, on_progress=None):
        calls.append({"headed": headed, "username": username, "password": password})
        return results[len(calls) - 1]

    monkeypatch.setattr("ght.auth_login._attempt", fake)
    monkeypatch.setattr("ght.auth_login._already_signed_in", lambda *a, **k: False)
    return calls, LoginResult


def test_an_unattended_sign_in_never_opens_a_window(monkeypatch):
    from ght.auth_login import LoginResult, perform_login

    calls, _ = _record_attempts(monkeypatch, [LoginResult(True, "ok", "signed in automatically")])
    monkeypatch.setattr("ght.auth_login.credentials_for", lambda slug: _creds("u", "p"))

    result = perform_login(_login_config())
    assert result.ok is True
    assert [c["headed"] for c in calls] == [False]


def test_a_captcha_hands_over_to_the_window_rather_than_being_worked_around(monkeypatch):
    """The whole point of the fallback. A challenge is not something to defeat: the
    unattended attempt stops, and a person finishes in a window that is already filled in."""
    from ght.auth_login import LoginResult, perform_login

    calls, _ = _record_attempts(
        monkeypatch,
        [
            LoginResult(False, "challenge", "a CAPTCHA or 2FA prompt appeared"),
            LoginResult(True, "ok", "signed in through the login window"),
        ],
    )
    monkeypatch.setattr("ght.auth_login.credentials_for", lambda slug: _creds("u", "p"))

    said = []
    result = perform_login(_login_config(), on_progress=lambda u: said.append(u.message))
    assert result.ok is True
    assert [c["headed"] for c in calls] == [False, True]
    # The window gets the credentials too, so only the challenge is left to do.
    assert calls[1]["username"] == "u"
    assert any("CAPTCHA" in m for m in said)


def test_without_credentials_it_behaves_exactly_as_it_did(monkeypatch):
    from ght.auth_login import LoginResult, perform_login

    calls, _ = _record_attempts(monkeypatch, [LoginResult(True, "ok", "signed in")])
    monkeypatch.setattr("ght.auth_login.credentials_for", lambda slug: _creds("", ""))

    result = perform_login(_login_config())
    assert result.ok is True
    assert [c["headed"] for c in calls] == [True]


def test_a_headless_only_site_without_credentials_says_which_variables_to_set(monkeypatch):
    from ght.auth_login import perform_login
    from ght.sources import Login, SourceConfig

    monkeypatch.setattr("ght.auth_login.credentials_for", lambda slug: _creds("", ""))
    config = SourceConfig(
        slug="quiet-site",
        name="x",
        fetcher="browser",
        auth_state="data/auth/x.json",
        login=Login(url="u", username="#u", password="#p", submit="#s", success=".ok"),
    )
    result = perform_login(config)
    assert result.ok is False
    assert "GHT_LOGIN_QUIET_SITE_PASSWORD" in result.detail


def _creds(username, password):
    from ght.credentials import Credentials

    return Credentials(username=username, password=password)


def test_credentials_are_read_from_the_env_file_not_just_the_process(tmp_path, monkeypatch):
    """Settings reads .env through pydantic, which never puts it into os.environ. Reading
    the process environment alone would silently ignore the file the operator edited."""
    import ght.config
    from ght.credentials import for_site

    (tmp_path / ".env").write_text(
        "GHT_LOGIN_1XBET_BD_USERNAME=from-file\nGHT_LOGIN_1XBET_BD_PASSWORD=also-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ght.config, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("GHT_LOGIN_1XBET_BD_USERNAME", raising=False)
    monkeypatch.delenv("GHT_LOGIN_1XBET_BD_PASSWORD", raising=False)

    found = for_site("1xbet-bd")
    assert (found.username, found.password) == ("from-file", "also-from-file")


def test_a_real_environment_variable_beats_the_env_file(tmp_path, monkeypatch):
    import ght.config
    from ght.credentials import for_site

    (tmp_path / ".env").write_text("GHT_LOGIN_X_USERNAME=from-file\n", encoding="utf-8")
    monkeypatch.setattr(ght.config, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("GHT_LOGIN_X_USERNAME", "from-env")
    monkeypatch.setenv("GHT_LOGIN_X_PASSWORD", "p")

    assert for_site("x").username == "from-env"
