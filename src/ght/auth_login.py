"""Credential login, saving the session the collector reuses.

Two modes:

* **Headless** (the default) fills the supplied credentials and submits, watching for a
  success marker. If a CAPTCHA or 2FA appears it aborts — those cannot be solved
  automatically, and trying to would be pointless.

* **Assisted** (``login.assisted: true``) opens a *visible* browser on the operator's
  machine, pre-fills the credentials, and then waits for the operator to finish signing in
  by hand — solving the CAPTCHA and pressing the site's login button themselves. When the
  success marker appears the session is captured. This is the reliable path for sites with
  bot protection, at the cost of needing a person at the machine.

Either way the credentials are typed once and never logged, and the saved session is
written in the wrapped format the browser fetcher reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ght.config import settings
from ght.sources import SourceConfig

# How long the assisted flow waits for a human to finish. Generous: a person may need to
# solve a CAPTCHA, receive a 2FA code, and click through.
ASSISTED_WAIT_SECONDS = 300

# Browsers to try, in order, when opening a window. Playwright's bundled Chromium first,
# then the ones already installed — security software often blocks the bundled build.
BROWSER_CHANNELS = (None, "msedge", "chrome")


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    reason: str  # "ok" | "challenge" | "bad_credentials" | "timeout" | "config" | "browser" | "error"
    detail: str = ""


def _save_state(path: str, state: dict, user_agent: str, channel: str | None) -> None:
    """Persist the session in the wrapped format BrowserFetcher._auth_kwargs understands."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"storage_state": state, "user_agent": user_agent, "channel": channel},
            indent=2,
        ),
        encoding="utf-8",
    )


def _visible(page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible(timeout=800)
    except Exception:  # noqa: BLE001 - absent or detached counts as not visible
        return False


def _launch(playwright, channel: str | None, headed: bool):
    """Open a browser, trying each channel until one starts. Returns (browser, channel)."""
    channels = (channel,) if channel else BROWSER_CHANNELS
    failures = []
    for candidate in channels:
        try:
            kwargs = {"channel": candidate} if candidate else {}
            browser = playwright.chromium.launch(headless=not headed, **kwargs)
            return browser, candidate
        except Exception as exc:  # noqa: BLE001 - trying the next browser is the point
            failures.append(f"{candidate or 'bundled chromium'} ({str(exc).splitlines()[0]})")
    raise RuntimeError("no usable browser: " + "; ".join(failures))


def perform_login(config: SourceConfig, username: str = "", password: str = "") -> LoginResult:
    """Sign in and save the session. Never raises for an expected failure — returns why.

    Credentials are optional: in assisted mode the operator can type them into the visible
    window themselves, so they are only pre-filled when supplied.
    """
    login = config.login
    if login is None:
        return LoginResult(False, "config", "no login block configured for this site")
    if not config.auth_state:
        return LoginResult(False, "config", "no auth_state path configured to save into")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return LoginResult(False, "browser", 'playwright not installed; pip install -e ".[browser]"')

    assisted = login.assisted
    timeout_ms = (config.timeout or settings.request_timeout) * 1000

    try:
        with sync_playwright() as pw:
            try:
                browser, used_channel = _launch(pw, config.browser_channel, headed=assisted)
            except RuntimeError as exc:
                return LoginResult(False, "browser", str(exc))

            context = browser.new_context(
                user_agent=settings.user_agent,
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            try:
                page.goto(login.url, timeout=timeout_ms, wait_until="domcontentloaded")

                if assisted:
                    outcome = _assisted(page, login, username, password, timeout_ms)
                else:
                    outcome = _headless(page, login, username, password, timeout_ms)

                if not outcome.ok:
                    return outcome

                state = context.storage_state()
                user_agent = page.evaluate("navigator.userAgent")
                _save_state(config.auth_state, state, user_agent, used_channel)
                return LoginResult(True, "ok", "signed in and saved the session")
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # noqa: BLE001 - any browser failure is a reported result
        return LoginResult(False, "error", f"{type(exc).__name__}: {exc}")


def _open_form(page, login, timeout_ms: int) -> None:
    if login.open:
        page.click(login.open, timeout=timeout_ms)


def _fill_form(page, login, username: str, password: str, timeout_ms: int) -> None:
    """Best-effort: open the form and pre-fill it. Failures are swallowed in assisted mode."""
    _open_form(page, login, timeout_ms)
    if username:
        page.fill(login.username, username, timeout=timeout_ms)
    if password:
        page.fill(login.password, password, timeout=timeout_ms)


def _headless(page, login, username: str, password: str, timeout_ms: int) -> LoginResult:
    """Fill, submit, and watch for success or a challenge we cannot pass."""
    if any(_visible(page, sel) for sel in login.challenge):
        return LoginResult(False, "challenge", "challenge shown on the login page")

    _fill_form(page, login, username, password, timeout_ms)
    page.click(login.submit, timeout=timeout_ms)

    waited = 0
    while waited <= timeout_ms:
        if any(_visible(page, sel) for sel in login.challenge):
            return LoginResult(False, "challenge", "a CAPTCHA or 2FA prompt appeared")
        if _visible(page, login.success):
            return LoginResult(True, "ok")
        page.wait_for_timeout(500)
        waited += 500

    if _visible(page, login.password):
        return LoginResult(False, "bad_credentials", "still on the login form after submit")
    return LoginResult(False, "error", f"success selector {login.success!r} never appeared")


def _assisted(page, login, username: str, password: str, timeout_ms: int) -> LoginResult:
    """Pre-fill, then wait for the operator to finish signing in in the visible window.

    The CAPTCHA and the final submit are the operator's to do; this only fills the fields
    to save typing and then watches for the success marker. Challenges are *not* treated as
    failures here — the whole point is that a human is present to clear them.
    """
    try:
        if username or password:
            _fill_form(page, login, username, password, timeout_ms)
        else:
            _open_form(page, login, timeout_ms)
    except Exception:  # noqa: BLE001, S110 - the operator can open and fill it themselves
        pass  # pre-fill is a convenience; the person can do it all in the window

    deadline = ASSISTED_WAIT_SECONDS * 1000
    waited = 0
    while waited <= deadline:
        if _visible(page, login.success):
            return LoginResult(True, "ok")
        page.wait_for_timeout(1000)
        waited += 1000
    return LoginResult(
        False,
        "timeout",
        f"no successful sign-in within {ASSISTED_WAIT_SECONDS}s in the browser window",
    )
