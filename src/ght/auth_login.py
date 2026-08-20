"""Signing in, and saving the session the collector reuses.

A run starts here. The saved session is first checked *headlessly*, by loading the page
collection actually needs and asking whether a signed-in user got served — and when the
session is still good nothing is shown at all and collection carries straight on.

Only a genuinely dead session brings up a browser:

* **Assisted** (``login.assisted: true``) opens a *visible* window and waits for the
  operator to sign in by hand, solving the CAPTCHA themselves. This is the only thing that
  reliably gets past bot protection, at the cost of needing a person at the machine.

* **Headless** (the default) fills supplied credentials and submits, watching for a success
  marker. If a CAPTCHA or 2FA appears it gives up — those cannot be solved automatically.

No credentials are stored; in assisted mode the operator types them into the window. The
saved session is written in the wrapped format the browser fetcher reads.
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
    """Open a browser via the shared resolver. Returns (browser, identity)."""
    from ght.browserlaunch import open_browser

    return open_browser(playwright, headed=headed, preferred=channel)


def perform_login(
    config: SourceConfig,
    username: str = "",
    password: str = "",
    on_progress=None,
) -> LoginResult:
    """Sign in and save the session. Never raises for an expected failure — returns why.

    Credentials are optional: in assisted mode the operator can type them into the visible
    window themselves, so they are only pre-filled when supplied. ``on_progress`` receives
    updates, which matters most here — this is the step that waits on a human.
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

    # Check the existing session without showing anything. Opening a window only to close
    # it a second later reads as a glitch, so the visible browser is reserved for the case
    # that actually needs a person: being signed out.
    if assisted and _already_signed_in(config, timeout_ms, on_progress):
        return LoginResult(True, "ok", "already signed in")

    if assisted:
        from ght.progress import report

        report(on_progress, "signin", "Signed out — opening a browser window for you")

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
                    outcome = _assisted(page, config, username, password, timeout_ms, on_progress)
                else:
                    outcome = _headless(page, login, username, password, timeout_ms)

                if not outcome.ok:
                    return outcome

                # Prove it before saving: load the page collection actually needs. Declaring
                # success on a login-form heuristic is how a logged-out session gets saved
                # and every probe then fails.
                page.goto(_target_url(config), timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                if not _page_is_signed_in(page, config):
                    return LoginResult(
                        False,
                        "error",
                        "the sign-in did not take — the deposit page still loads signed out",
                    )

                state = context.storage_state()
                user_agent = page.evaluate("navigator.userAgent")
                _save_state(config.auth_state, state, user_agent, used_channel)
                return LoginResult(True, "ok", "signed in and saved the session")
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # noqa: BLE001 - any browser failure is a reported result
        return LoginResult(False, "error", f"{type(exc).__name__}: {exc}")


def _target_url(config: SourceConfig) -> str:
    """The page whose accessibility actually decides whether we are signed in."""
    urls = config.current_urls
    return urls[0] if urls else config.login.url


def _page_is_signed_in(page, config: SourceConfig) -> bool:
    """Judge a loaded page the same way the collector does.

    Checking the *deposit* page rather than the login page matters: a site can bounce an
    anonymous visitor off its login URL too, so "we left the login page" is not evidence of
    anything. Being served the page we actually want is.
    """
    url = page.url or ""
    if config.logged_out_url and config.logged_out_url in url:
        return False
    if "/login" in url:
        return False
    marker = config.logged_out_marker
    if marker:
        try:
            if marker in page.content():
                return False
        except Exception:  # noqa: BLE001 - an unreadable page proves nothing either way
            return False
    return True


def _already_signed_in(config: SourceConfig, timeout_ms: int, on_progress=None) -> bool:
    """Is the saved session still good? Checked headlessly, so nothing flashes on screen.

    Returns False on any error — the visible window is then the fallback, and a wrong "no"
    costs a sign-in rather than a broken run.
    """
    from playwright.sync_api import sync_playwright

    from ght.browserlaunch import open_browser
    from ght.progress import report

    try:
        with sync_playwright() as pw:
            browser, used_channel = open_browser(
                pw, headed=False, preferred=config.browser_channel
            )
            context = _restore_context(browser, config.auth_state)
            page = context.new_page()
            try:
                page.goto(_target_url(config), timeout=timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                signed_in = _page_is_signed_in(page, config)
                if signed_in and config.auth_state:
                    report(on_progress, "signin", "Already signed in")
                    # Save the refreshed cookies so the session keeps rolling forward.
                    _save_state(
                        config.auth_state,
                        context.storage_state(),
                        page.evaluate("navigator.userAgent"),
                        used_channel,
                    )
                return signed_in
            finally:
                context.close()
                browser.close()
    except Exception:  # noqa: BLE001 - fall back to asking the operator
        return False


def _restore_context(browser, auth_state: str | None):
    """A browser context carrying the saved session, if there is one to carry.

    The session's own User-Agent is reused: Cloudflare ties its clearance cookie to the UA
    that earned it, so replaying cookies under a different one reads as a new device and
    the check would wrongly conclude we are signed out.
    """
    kwargs = {
        "user_agent": settings.user_agent,
        "locale": "en-US",
        "viewport": {"width": 1366, "height": 900},
    }
    if auth_state:
        path = Path(auth_state)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and "storage_state" in data:
                kwargs["storage_state"] = data["storage_state"]
                if data.get("user_agent"):
                    kwargs["user_agent"] = data["user_agent"]
            elif data is not None:
                kwargs["storage_state"] = data
    return browser.new_context(**kwargs)


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


def _assisted(
    page,
    config: SourceConfig,
    username: str,
    password: str,
    timeout_ms: int,
    on_progress=None,
) -> LoginResult:
    """Wait for the operator to sign in in the visible window.

    The CAPTCHA and the submit are theirs to do; this opens the form, optionally pre-fills
    it, and then watches. A challenge is *not* a failure here — a person is present to
    clear it.

    Success is judged by the same test the collector uses: is this page one a signed-in
    user gets? Nothing weaker works. A site bounces anonymous visitors off its login URL
    too, so "we left the login page" proves nothing, and a stray element can satisfy a
    success selector on the logged-out page — both of which end with a logged-out session
    being saved and every probe failing afterwards.
    """
    login = config.login
    try:
        if username or password:
            _fill_form(page, login, username, password, timeout_ms)
        else:
            _open_form(page, login, timeout_ms)
    except Exception:  # noqa: BLE001, S110 - the operator can open and fill it themselves
        pass  # pre-fill is a convenience; the person can do it all in the window

    from ght.progress import report

    report(on_progress, "signin", "Waiting for you to sign in — a browser window is open")

    deadline = ASSISTED_WAIT_SECONDS * 1000
    waited = 0
    while waited <= deadline:
        if _page_is_signed_in(page, config) and _visible(page, login.success):
            report(on_progress, "signin", "Signed in")
            # Give redirects and post-login cookies a moment to settle before we snapshot.
            page.wait_for_timeout(1500)
            return LoginResult(True, "ok", "signed in through the login window")

        # Count down out loud, so the window never looks like it has stalled.
        if waited and waited % 15000 == 0:
            left = (deadline - waited) // 1000
            report(on_progress, "signin", f"Still waiting for you to sign in — {left}s left")
        page.wait_for_timeout(1000)
        waited += 1000
    return LoginResult(
        False,
        "timeout",
        f"no successful sign-in within {ASSISTED_WAIT_SECONDS}s in the browser window",
    )
