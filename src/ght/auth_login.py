"""Signing in, and saving the session the collector reuses.

A run starts here. The saved session is first checked *headlessly*, by loading the page
collection actually needs and asking whether a signed-in user got served — and when the
session is still good nothing is shown at all and collection carries straight on.

Only a genuinely dead session brings up a browser:

* **Assisted** (``login.assisted: true``) opens a *visible* window and waits for the
  operator to sign in by hand, solving the CAPTCHA themselves. This is the only thing that
  reliably gets past bot protection, at the cost of needing a person at the machine.

* **Headless** fills credentials and submits, watching for a success marker. If a CAPTCHA
  or 2FA appears it gives up — those cannot be solved automatically, and this does not try.

An assisted site takes both, in that order: the unattended attempt runs first, and the
window only opens when the site actually challenges it. On a day the challenge does not
appear, nobody is needed at all; on a day it does, the window arrives pre-filled with
only the CAPTCHA left to do.

Credentials come from the environment (see ``ght.credentials``) and are never written to
the source configs, the database, a log line or a run report. The saved session is written
in the wrapped format the browser fetcher reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ght.config import settings
from ght.credentials import env_names
from ght.credentials import for_site as credentials_for
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

    The order is deliberate, cheapest and least intrusive first:

    1. Is the saved session still good? Then nothing happens at all.
    2. With credentials configured, try to sign in **unattended** — fill, submit, watch.
       No window, nobody needed. If the site answers with a CAPTCHA or 2FA this stops and
       says so; it is never treated as something to get around.
    3. Only then, on an assisted site, open the visible window — pre-filled from the same
       credentials, so what is left for the person is the challenge and the button.

    ``on_progress`` receives updates, which matters most here: this is the step that can
    end up waiting on a human.
    """
    login = config.login
    if login is None:
        return LoginResult(False, "config", "no login block configured for this site")
    if not config.auth_state:
        return LoginResult(False, "config", "no auth_state path configured to save into")

    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return LoginResult(False, "browser", 'playwright not installed; pip install -e ".[browser]"')

    from ght.progress import report

    assisted = login.assisted
    timeout_ms = (config.timeout or settings.request_timeout) * 1000

    # Check the existing session without showing anything. Opening a window only to close
    # it a second later reads as a glitch, so the visible browser is reserved for the case
    # that actually needs a person: being signed out.
    if assisted and _already_signed_in(config, timeout_ms, on_progress):
        return LoginResult(True, "ok", "already signed in")

    if not username and not password:
        credentials = credentials_for(config.slug)
        username, password = credentials.username, credentials.password

    if username and password:
        report(on_progress, "signin", "Signing in")
        outcome = _attempt(config, username, password, timeout_ms, headed=False)
        if outcome.ok:
            return outcome
        if not assisted:
            return outcome
        # A challenge is the expected answer from a site that shows one, not a fault.
        report(
            on_progress,
            "signin",
            "The site asked for a CAPTCHA — opening a window for you"
            if outcome.reason == "challenge"
            else f"Could not sign in unattended ({outcome.reason}) — opening a window for you",
        )
    elif assisted:
        report(on_progress, "signin", "Signed out — opening a browser window for you")

    if not assisted:
        return LoginResult(
            False,
            "config",
            f"no credentials configured for {config.slug}: set {' and '.join(env_names(config.slug))}",
        )

    return _attempt(config, username, password, timeout_ms, headed=True, on_progress=on_progress)


def _attempt(
    config: SourceConfig,
    username: str,
    password: str,
    timeout_ms: int,
    headed: bool,
    on_progress=None,
) -> LoginResult:
    """One sign-in attempt in its own browser, saving the session if it took.

    ``headed`` picks which half of the job this is: a hidden browser filling the form
    itself, or a visible one a person finishes. Everything after the form — proving the
    session on the page collection needs, and saving it — is the same either way, and is
    what keeps a login-form heuristic from being mistaken for a working session.
    """
    from playwright.sync_api import sync_playwright

    login = config.login
    try:
        with sync_playwright() as pw:
            try:
                browser, used_channel = _launch(pw, config.browser_channel, headed=headed)
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

                if headed:
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
                if not _page_is_signed_in(page, config, require_frame=True):
                    return LoginResult(
                        False,
                        "error",
                        "the sign-in did not take — the deposit page still loads signed out",
                    )

                state = context.storage_state()
                user_agent = page.evaluate("navigator.userAgent")
                _save_state(config.auth_state, state, user_agent, used_channel)
                return LoginResult(
                    True,
                    "ok",
                    "signed in through the login window"
                    if headed
                    else "signed in automatically and saved the session",
                )
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # noqa: BLE001 - any browser failure is a reported result
        return LoginResult(False, "error", f"{type(exc).__name__}: {exc}")


def _target_url(config: SourceConfig) -> str:
    """The page whose accessibility actually decides whether we are signed in."""
    urls = config.current_urls
    return urls[0] if urls else config.login.url


# How long the session check waits for the embedded payment app before calling the session
# dead. Shorter than the collector's own timeout on purpose: this runs before every run, and
# a session that needs longer than this to prove itself is not one to collect on.
FRAME_WAIT_MS = 20_000


def _safe_content(page) -> str | None:
    """``page.content()``, or None while the page is mid-navigation."""
    try:
        return page.content()
    except Exception:  # noqa: BLE001 - an unreadable page proves nothing either way
        return None


def _payment_frame_appears(page, config: SourceConfig, timeout_ms: int = FRAME_WAIT_MS) -> bool:
    """Whether the embedded payment app actually loads for this session.

    This is the difference between the session check and the collection. 1xBet serves the
    account page shell to an expired session — no redirect, no logged-out marker — and only
    refuses when the payment iframe is requested. Checking the shell alone produced runs
    that reported "already signed in" and then failed on the first probe with "the site
    signed us out", which reads as a contradiction because it is one: two different
    questions were being asked.

    Waiting for the frame asks the collector's question. A frame that never comes is
    treated as signed out, which costs at worst an unnecessary sign-in window — far
    cheaper than a run that dies eight probes later with nothing collected.
    """
    step = 500
    waited = 0
    while waited <= timeout_ms:
        for frame in page.frames:
            if config.frame in (frame.url or ""):
                return _panel_accepts_session(page, config)
        # The logged-out layout can also appear late, once the site has decided about the
        # session. Bail the moment it does rather than waiting out the whole window.
        marker = config.logged_out_marker
        if marker and marker in (_safe_content(page) or ""):
            return False
        page.wait_for_timeout(step)
        waited += step
    return False


# The refusal dialog is drawn a beat after the panel appears, so a check that reads the
# page the instant the frame exists sees a clean one. Long enough to catch it, short enough
# that a healthy session is still proven in about a second.
REFUSAL_GRACE_MS = 2500


def _panel_accepts_session(page, config: SourceConfig) -> bool:
    """Whether the loaded payment app will actually serve this session.

    The frame appearing is not the whole answer. These sites embed the panel for an expired
    session too, render the full method list inside it, and then cover the lot with their
    own "the session has expired" dialog — drawn on the page, not in the frame. Every click
    lands on that dialog, so a run that starts here spends its whole budget being refused
    and reports twelve broken selectors.

    Treating the refusal as signed-out costs one sign-in window and collects; believing the
    frame costs a run.
    """
    if not config.session_expired:
        return True
    waited = 0
    while waited < REFUSAL_GRACE_MS:
        if any(_visible(page, selector) for selector in config.session_expired):
            return False
        page.wait_for_timeout(250)
        waited += 250
    return True


def _visible(page, selector: str) -> bool:
    """Whether the selector matches something actually on screen.

    Presence proves nothing here: the dialog's markup sits in the page either way, and only
    its being up means the panel is refusing us.
    """
    try:
        handle = page.query_selector(selector)
        return handle is not None and handle.is_visible()
    except Exception:  # noqa: BLE001 - unreadable is not visible
        return False


def _page_is_signed_in(page, config: SourceConfig, require_frame: bool = False) -> bool:
    """Judge a loaded page the same way the collector does.

    Checking the *deposit* page rather than the login page matters: a site can bounce an
    anonymous visitor off its login URL too, so "we left the login page" is not evidence of
    anything. Being served the page we actually want is.

    ``require_frame`` adds the collector's real test — that the embedded payment app
    loads — and belongs only where the page being judged *is* the deposit page. In the
    assisted window the operator can land anywhere the site sends them after sign-in, and
    demanding a deposit iframe on the homepage would reject a perfectly good sign-in.
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
    if require_frame and config.frame:
        return _payment_frame_appears(page, config)
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
                signed_in = _page_is_signed_in(page, config, require_frame=True)
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
