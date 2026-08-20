"""Headless credential login, saving the session the collector reuses.

Given a site's login config and its decrypted credentials, this drives a headless browser
through the sign-in form and, on success, writes the session to the site's ``auth_state``
file in the same wrapped format the browser fetcher reads.

It does not, and will not, defeat bot protection: if a CAPTCHA or a 2FA prompt appears, the
attempt aborts with ``reason="challenge"`` so the operator knows to capture that site's
session by hand instead. Credentials are typed into the page and never logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ght.config import settings
from ght.fetchers.browser import BrowserFetcher
from ght.sources import SourceConfig


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    reason: str  # "ok" | "challenge" | "bad_credentials" | "config" | "browser" | "error"
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


def perform_login(config: SourceConfig, username: str, password: str) -> LoginResult:
    """Sign in and save the session. Never raises for an expected failure — returns why."""
    login = config.login
    if login is None:
        return LoginResult(False, "config", "no login block configured for this site")
    if not config.auth_state:
        return LoginResult(False, "config", "no auth_state path configured to save into")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return LoginResult(False, "browser", 'playwright not installed; pip install -e ".[browser]"')

    timeout_ms = (config.timeout or settings.request_timeout) * 1000
    fetcher = BrowserFetcher(channel=config.browser_channel)

    try:
        with sync_playwright() as pw:
            try:
                browser = fetcher._launch(pw)
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

                # A challenge can appear before the form is even usable.
                if any(_visible(page, sel) for sel in login.challenge):
                    return LoginResult(False, "challenge", "challenge shown on the login page")

                if login.open:
                    page.click(login.open, timeout=timeout_ms)

                page.fill(login.username, username, timeout=timeout_ms)
                page.fill(login.password, password, timeout=timeout_ms)
                page.click(login.submit, timeout=timeout_ms)

                outcome = _await_outcome(page, login, timeout_ms)
                if not outcome.ok:
                    return outcome

                state = context.storage_state()
                user_agent = page.evaluate("navigator.userAgent")
                _save_state(config.auth_state, state, user_agent, fetcher._used_channel)
                return LoginResult(True, "ok", "signed in and saved the session")
            finally:
                context.close()
                browser.close()
    except Exception as exc:  # noqa: BLE001 - any browser failure is a reported result
        return LoginResult(False, "error", f"{type(exc).__name__}: {exc}")


def _await_outcome(page, login, timeout_ms: int) -> LoginResult:
    """Poll for the success marker, a challenge, or a stuck form, whichever comes first."""
    waited = 0
    step = 500
    while waited <= timeout_ms:
        if any(_visible(page, sel) for sel in login.challenge):
            return LoginResult(False, "challenge", "a CAPTCHA or 2FA prompt appeared")
        if _visible(page, login.success):
            return LoginResult(True, "ok")
        page.wait_for_timeout(step)
        waited += step

    # Still on the form after the timeout usually means the credentials were rejected;
    # otherwise the success selector has gone stale. Either way there is nothing to save.
    if _visible(page, login.password):
        return LoginResult(False, "bad_credentials", "still on the login form after submit")
    return LoginResult(False, "error", f"success selector {login.success!r} never appeared")
