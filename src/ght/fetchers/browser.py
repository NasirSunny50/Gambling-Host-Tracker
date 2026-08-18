"""Headless browser fetcher for sites that build the deposit panel in JavaScript.

Playwright is an optional dependency (``pip install -e ".[browser]"`` plus
``playwright install chromium``), so it is imported lazily — the HTTP fetcher and the whole
test suite work without it installed.

This fetcher also captures a screenshot, which is the part of the evidence bundle a
non-technical reviewer can actually read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ght.config import settings
from ght.sources import Step
from ght.types import RawCapture

# Query parameters that carry a live credential. Embedded payment apps routinely take a
# short-lived JWT in the URL, and that URL is stored on the run and shown in exports - so
# it would put a working session token in front of everyone reading the AML reports.
SECRET_QUERY_KEYS = frozenset(
    {"h_token", "token", "access_token", "auth", "sig", "signature", "session", "key"}
)


def redact_url(url: str) -> str:
    """Replace the value of any credential-bearing query parameter with REDACTED."""
    if not url or "?" not in url:
        return url
    parts = urlsplit(url)
    pairs = [
        (key, "REDACTED" if key.lower() in SECRET_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(pairs)))


class BrowserFetcher:
    name = "browser"

    # Playwright's bundled Chromium is preferred, but on locked-down Windows machines
    # security software quarantines it without warning - it is a large unsigned binary that
    # launches from a temp profile. The browsers already installed are trusted and work
    # identically for our purposes, so fall through to them rather than failing the run.
    BROWSER_CHANNELS = (None, "msedge", "chrome")

    def __init__(
        self,
        timeout: int | None = None,
        wait_for: str | None = None,
        screenshot: bool = True,
        flow: list[Step] | None = None,
        auth_state: str | None = None,
        channel: str | None = None,
        frame: str | None = None,
    ) -> None:
        self.timeout = (timeout if timeout is not None else settings.request_timeout) * 1000
        # Optional selector to wait for, so we capture after the deposit panel renders
        # rather than at first paint.
        self.wait_for = wait_for
        self.screenshot = screenshot
        # Clicks to walk before capturing, for sites that only reveal the account on a
        # later page. Empty for the common case of everything being on the deposit page.
        self.flow = flow or []
        self.auth_state = auth_state
        # Pin a specific browser, or leave unset to try each in turn.
        self.channel = channel
        # Filled in from the session file: the browser identity that created the session.
        self._session_user_agent: str | None = None
        self._session_channel: str | None = None
        # URL substring of the iframe to work inside, when the deposit UI is embedded.
        self.frame = frame

    def _auth_kwargs(self) -> tuple[dict, str | None]:
        """Reuse a saved browser session when the deposit page sits behind a login.

        The state file is exported by a person who signed in themselves; this fetcher never
        handles credentials. Returns the context kwargs plus a warning when the session
        could not be used.

        Neither a missing nor an unreadable state file is fatal: the run continues logged
        out and captures whatever an anonymous visitor sees. That is a real observation,
        and it beats failing the whole collection over an expired session file.
        """
        if not self.auth_state:
            return {}, None
        path = Path(self.auth_state)
        if not path.exists():
            return {}, f"auth_state {path} not found; continuing logged out"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}, f"auth_state {path} is not readable session JSON ({type(exc).__name__})"

        # Two shapes are accepted. Ours wraps the Playwright state so the browser identity
        # that produced it travels with it; a bare Playwright export is still honoured.
        if isinstance(data, dict) and "storage_state" in data:
            state = data["storage_state"]
            self._session_user_agent = data.get("user_agent")
            self._session_channel = data.get("channel")
        else:
            state = data
            self._session_user_agent = None
            self._session_channel = None

        return {"storage_state": state}, None

    def _walk(self, page) -> str | None:
        """Click through the configured flow. Returns an error string if a step fails.

        A failed step still lets the capture through: the partial page is evidence that the
        flow broke, and pairing it with the screenshot is how someone works out which
        button moved.
        """
        for index, step in enumerate(self.flow, start=1):
            try:
                page.wait_for_selector(step.target, timeout=self.timeout)
                if step.select:
                    page.select_option(step.select, label=step.option, timeout=self.timeout)
                else:
                    page.click(step.click, timeout=self.timeout)
                if step.wait_for:
                    page.wait_for_selector(step.wait_for, timeout=self.timeout)
                else:
                    page.wait_for_load_state("networkidle", timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - a broken step is a reportable result
                if step.optional:
                    continue
                return f"flow step {index} ({step.target!r}) failed: {type(exc).__name__}: {exc}"
        return None

    def _launch(self, playwright):
        """Return a browser, trying each channel until one starts.

        Raises the collected failures as one error when none of them do, so the run report
        names every browser that was tried instead of only the first.
        """
        # A session is tied to the browser that made it, so prefer that one.
        preferred = self.channel or self._session_channel
        channels = (preferred,) if preferred else self.BROWSER_CHANNELS
        failures = []
        for candidate in channels:
            try:
                kwargs = {"channel": candidate} if candidate else {}
                return playwright.chromium.launch(headless=True, **kwargs)
            except Exception as exc:  # noqa: BLE001 - trying the next browser is the point
                label = candidate or "bundled chromium"
                failures.append(f"{label} ({str(exc).splitlines()[0]})")
        raise RuntimeError("no usable browser: " + "; ".join(failures))

    def _target(self, page) -> tuple[object, str | None]:
        """The document the flow and the capture belong to.

        Without a configured frame that is the page itself. With one, it is the embedded
        payment app: clicks and selectors aimed at the top-level document silently match
        nothing there, which looks identical to a site that changed its markup.
        """
        if not self.frame:
            return page, None

        deadline = self.timeout
        step = 500
        waited = 0
        while waited <= deadline:
            for frame in page.frames:
                if self.frame in frame.url:
                    return frame, None
            page.wait_for_timeout(step)
            waited += step
        return page, f"frame matching {self.frame!r} never appeared; captured the top page"

    def _capture_target(self, page):
        """Re-resolve the document to capture, after the flow has run.

        The frame handle used for clicking does not survive the flow: confirming a deposit
        navigates the embedded app, which detaches the old frame, and the step may even
        move the whole tab to the provider's own page. So the target is looked up again,
        falling back to the top-level page when the frame is gone.
        """
        if self.frame:
            for frame in page.frames:
                if self.frame in frame.url:
                    return frame
        return page

    def _settle(self, page, skip_wait_for: bool = False) -> str | None:
        """Wait for the finished page, without letting that wait discard the capture.

        A `wait_for` that never matches is a broken selector, not a failed fetch: the page
        we did land on is exactly what someone needs to see to fix the config. So a timeout
        here is reported and the capture proceeds.

        When the flow already broke we are knowingly on the wrong page, so waiting for a
        selector that only exists on the right one would just burn the timeout twice.
        """
        try:
            if self.wait_for and not skip_wait_for:
                page.wait_for_selector(self.wait_for, timeout=self.timeout)
            else:
                page.wait_for_load_state("networkidle", timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - a stale wait selector is reportable
            if skip_wait_for:
                return None  # the flow error already says what went wrong
            return f"wait_for {self.wait_for!r} never matched: {type(exc).__name__}"
        return None

    def fetch(self, url: str) -> RawCapture:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return RawCapture(
                url=url,
                status_code=0,
                fetcher=self.name,
                fetched_at=datetime.now(UTC),
                error='playwright not installed; pip install -e ".[browser]" '
                "&& playwright install chromium",
            )

        auth_kwargs, auth_warning = self._auth_kwargs()

        try:
            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                context = browser.new_context(
                    # Cloudflare binds its clearance cookie to the User-Agent that earned
                    # it, and the site fingerprints the browser besides. Replaying a saved
                    # session under our default UA reads as a different device and lands
                    # back on the logged-out page, so the session's own UA wins.
                    user_agent=self._session_user_agent or settings.user_agent,
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                    **auth_kwargs,
                )
                page = context.new_page()
                response = page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")

                target, frame_error = self._target(page)
                flow_error = None if frame_error else self._walk(target)

                target = self._capture_target(page)
                settle_error = self._settle(
                    target, skip_wait_for=frame_error is not None or flow_error is not None
                )

                capture = RawCapture(
                    url=redact_url(target.url),
                    status_code=response.status if response else 0,
                    html=target.content(),
                    screenshot=page.screenshot(full_page=True) if self.screenshot else None,
                    headers=dict(response.headers) if response else {},
                    fetcher=self.name,
                    fetched_at=datetime.now(UTC),
                    flow_error=frame_error or flow_error or settle_error or auth_warning,
                )
                context.close()
                browser.close()
                return capture
        except Exception as exc:  # noqa: BLE001 - any browser failure is just a failed run
            return RawCapture(
                url=url,
                status_code=0,
                fetcher=self.name,
                fetched_at=datetime.now(UTC),
                error=f"{type(exc).__name__}: {exc}",
            )
