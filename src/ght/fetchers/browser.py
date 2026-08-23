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


def _detached(frame) -> bool:
    """Whether a frame has been navigated away from and can no longer be read."""
    try:
        return bool(frame.is_detached())
    except Exception:  # noqa: BLE001 - a frame that cannot answer is not usable either
        return True


def _read_html(target, page) -> tuple[str, str | None]:
    """The captured document, falling back to the top page if the target went away.

    A flow that navigates out of an iframe can leave the target unreadable by the time we
    read it. The page we did land on is still the evidence for what happened, so capture
    that rather than losing the whole probe.
    """
    try:
        return target.content(), None
    except Exception:  # noqa: BLE001, S110 - fall back to whatever document survived
        pass  # the frame is gone; the page below is still evidence of where we ended up
    try:
        return page.content(), "captured the top page: the frame went away before it was read"
    except Exception as exc:  # noqa: BLE001 - nothing readable at all
        return "", f"page could not be read: {type(exc).__name__}"


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
        logged_out_marker: str | None = None,
        unavailable: list[str] | None = None,
        flow_timeout: int | None = None,
        reset=None,
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
        # The browser channel the last _launch actually used (None = bundled Chromium).
        self._used_channel: str | None = None
        # URL substring of the iframe to work inside, when the deposit UI is embedded.
        self.frame = frame
        # Substring that only appears while logged out, used to fail fast on a dead session.
        self.logged_out_marker = logged_out_marker
        # Selectors the site renders when it has switched a method off itself.
        self.unavailable = list(unavailable or [])
        # Clicks get their own, shorter budget: the panel may take a minute to appear, but
        # a button that exists appears in seconds, so a missing one must not cost the lot.
        self.flow_timeout = (flow_timeout * 1000) if flow_timeout is not None else self.timeout
        # Set by _walk when the site declared a method unavailable.
        self._unavailable_hit: str | None = None
        # Set by _walk when a step moved the flow into a different frame.
        self._entered_frame = None
        # How to close an open modal so the next probe can use the same loaded panel.
        self.reset = reset

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

    def _enter_frame(self, page, needle: str):
        """The frame a step wants, once it exists. None if it never turns up.

        Polled rather than assumed: the frame is created in answer to the click before it,
        and the document inside it arrives later still.
        """
        waited = 0
        while waited <= self.flow_timeout:
            for frame in page.frames:
                if needle in (frame.url or "") and not _detached(frame):
                    return frame
            page.wait_for_timeout(500)
            waited += 500
        return None

    def _walk(self, target, page=None) -> str | None:
        """Click through the configured flow. Returns an error string if a step fails.

        A failed step still lets the capture through: the partial page is evidence that the
        flow broke, and pairing it with the screenshot is how someone works out which
        button moved.

        A step may name a frame, which it and every step after it act inside. ``page`` is
        the tab those frames are looked up on; without one the flow simply stays where it
        started, which is every flow that has no frame to move into.
        """
        self._unavailable_hit = None
        self._entered_frame = None
        tab = page or target
        for index, step in enumerate(self.flow, start=1):
            try:
                if step.frame:
                    entered = self._enter_frame(tab, step.frame)
                    if entered is None:
                        return f"flow step {index}: no frame matching {step.frame!r} appeared"
                    target = entered
                    self._entered_frame = entered
                target.wait_for_selector(step.target, timeout=self.flow_timeout)
                if step.select:
                    missing = self._missing_option(target, step)
                    if missing is not None:
                        return f"flow step {index}: {missing}"
                    # force=True because these dropdowns are Select2 widgets: the real
                    # <select> is a 1x1 accessibility shim behind a styled replacement, and
                    # the usual actionability checks never pass on it.
                    target.select_option(
                        step.select, label=step.option, force=True, timeout=self.flow_timeout
                    )
                elif step.fill:
                    # Typed rather than assigned: these forms enable the next button from
                    # the input's own key events, and a value set straight onto the element
                    # leaves the button disabled and the step reported as a broken selector.
                    target.click(step.fill, timeout=self.flow_timeout)
                    target.fill(step.fill, "", timeout=self.flow_timeout)
                    target.type(step.fill, step.value, delay=40, timeout=self.flow_timeout)
                else:
                    target.click(step.click, timeout=self.flow_timeout)
                if step.wait_for:
                    # Race the expected panel against the site's own "unavailable" one.
                    # Waiting only for the expected one means a method the operator has
                    # switched off costs a full timeout and is then reported as our
                    # selector being broken, which is the wrong thing to go and fix.
                    self._wait_for_either(target, step.wait_for)
                    if self._unavailable_hit:
                        return None
                else:
                    target.wait_for_load_state("networkidle", timeout=self.flow_timeout)
            except Exception as exc:  # noqa: BLE001 - a broken step is a reportable result
                if step.optional:
                    continue
                return f"flow step {index} ({step.target!r}) failed: {type(exc).__name__}: {exc}"
        return None

    def _wait_for_either(self, page, wanted: str) -> None:
        """Wait for the panel we want or the one that says we cannot have it.

        Playwright takes a comma-joined selector as "any of these", so both are waited on
        in a single call and whichever renders first ends the wait. Only then do we look
        at which it was.
        """
        combined = ", ".join([wanted, *self.unavailable]) if self.unavailable else wanted
        page.wait_for_selector(combined, timeout=self.flow_timeout)

        # The panel we asked for wins, always. The site reuses one modal element for every
        # method and only swaps its classes once the new one has loaded, so between two
        # probes it is briefly visible still wearing the last method's "unavailable" class.
        # Believing that marker over the payee actually in front of us reported every
        # method after the first closed one as switched off, and collected nothing.
        if self._visible(page, wanted):
            return
        # Not there yet: give the modal a moment to finish swapping before taking the
        # marker at its word.
        page.wait_for_timeout(1000)
        if self._visible(page, wanted):
            return
        for marker in self.unavailable:
            if self._visible(page, marker):
                self._unavailable_hit = marker
                return

    @staticmethod
    def _visible(page, selector: str) -> bool:
        """Whether the selector matches something actually on screen.

        Presence is not enough: a closed modal keeps its markup, so a probe sharing the
        panel would keep matching the previous method's leftovers.
        """
        try:
            handle = page.query_selector(selector)
            return handle is not None and handle.is_visible()
        except Exception:  # noqa: BLE001 - unreadable is not visible
            return False

    def _missing_option(self, page, step) -> str | None:
        """Report a dropdown that no longer offers the configured option.

        Without this the run waits the full timeout for an option that will never appear —
        a site quietly changing its list of recipient banks costs ninety seconds per probe
        and reports only "timeout". Naming what the dropdown *does* offer turns that into a
        one-line config fix.
        """
        try:
            labels = page.eval_on_selector(
                step.select,
                "el => Array.from(el.options).map(o => o.text.trim())",
            )
        except Exception:  # noqa: BLE001 - not a <select> we can read; let Playwright try
            return None
        if step.option in labels:
            return None
        offered = ", ".join(repr(label) for label in labels) or "nothing"
        return f"{step.select!r} has no option {step.option!r}; it offers {offered}"

    def _launch(self, playwright):
        """Return a browser, trying each channel until one starts.

        Raises the collected failures as one error when none of them do, so the run report
        names every browser that was tried instead of only the first.
        """
        from ght.browserlaunch import open_browser

        # A session is tied to the browser that made it, so prefer that one.
        preferred = self.channel or self._session_channel
        browser, identity = open_browser(playwright, headed=False, preferred=preferred)
        # Remember which browser actually started, so a session saved from this launch can
        # be pinned to it on replay.
        self._used_channel = identity
        return browser

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
            # An expired login shows the logged-out page, where the frame will never come.
            # Bail the moment that is visible rather than waiting out the whole timeout.
            html = self._safe_content(page)
            if self.logged_out_marker and html and self.logged_out_marker in html:
                return page, "LOGGED_OUT"
            page.wait_for_timeout(step)
            waited += step
        return page, f"frame matching {self.frame!r} never appeared; captured the top page"

    @staticmethod
    def _safe_content(page) -> str | None:
        """``page.content()`` but ``None`` while the page is mid-navigation.

        A logged-out session redirects to the homepage, and content() raises during the
        redirect. The caller treats ``None`` as "not settled yet" and keeps polling.
        """
        try:
            return page.content()
        except Exception:  # noqa: BLE001 - transient navigation; None means "not yet"
            return None

    def _capture_target(self, page, navigated: bool = False):
        """Re-resolve the document to capture, after the flow has run.

        The frame handle used for clicking does not survive the flow: confirming a deposit
        navigates the embedded app, which detaches the old frame, and the step may even
        move the whole tab to the provider's own page. So the target is looked up again,
        falling back to the top-level page when the frame is gone.

        ``navigated`` says the flow took the whole tab somewhere else — the payee is on that
        new page, so the embedded app is no longer what we came for even if a frame by that
        name still exists on it.
        """
        if navigated:
            return page
        # A step that moved into another frame - a provider's own deposit form - is where
        # the probe ended up, so that is what to read. The configured frame is the panel it
        # started in, which by now holds something else entirely.
        if self._entered_frame is not None and not _detached(self._entered_frame):
            return self._entered_frame
        if self.frame:
            for frame in page.frames:
                # A frame the flow navigated away from can linger in page.frames while being
                # detached; reading from it throws. Only a live one is worth returning.
                if self.frame in frame.url and not _detached(frame):
                    return frame
        return page

    def _reset_panel(self, page) -> bool:
        """Close the open modal so the next probe can use the same loaded panel.

        Returns whether the panel is trustworthy for another probe. False means "reload" -
        never "carry on and hope". A half-closed modal swallows the next probe's click, and
        that would be recorded as *that* probe's selector being broken, sending someone to
        fix a config that was correct.
        """
        if self.reset is None:
            return False
        try:
            if page.query_selector(self.reset.gone) is None:
                return True  # nothing is open; the list is already reachable
            handle = page.query_selector(self.reset.click)
            if handle is None:
                return False
            handle.click(timeout=self.flow_timeout)
            page.wait_for_selector(self.reset.gone, state="detached", timeout=5000)
            return True
        except Exception:  # noqa: BLE001 - any doubt at all means reload instead
            return False

    def _looks_logged_out(self, html: str | None) -> bool:
        return bool(self.logged_out_marker and html and self.logged_out_marker in html)

    def _open_frame(self, page):
        """The embedded panel as it stands right now, without waiting for one to appear.

        Unlike ``_target`` this never blocks: it is asked between probes, where a missing
        frame simply means the page has to be reloaded anyway.
        """
        if not self.frame:
            return page
        for frame in page.frames:
            if self.frame in frame.url and not _detached(frame):
                return frame
        return page

    def fetch_many(self, urls: list[str], plans: list) -> list:
        """Walk several probes inside one loaded panel.

        Every probe needs the same method list, and the embedded panel takes ten to
        thirteen seconds to start each time it is loaded - which was the bulk of a run
        spent re-fetching a page we already had. So the browser, the page and the panel are
        opened once and each probe is walked inside them.

        Each returned capture still stands alone: its own HTML, its own screenshot, its own
        flow error. Whenever the panel cannot be proven clean between probes it is reloaded,
        so sharing it can cost time but cannot mix one method's payee up with another's.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return [self._no_playwright(urls[0] if urls else "") for _ in plans]

        auth_kwargs, auth_warning = self._auth_kwargs()
        captures: list = []
        try:
            with sync_playwright() as playwright:
                browser = self._launch(playwright)
                context = browser.new_context(
                    user_agent=self._session_user_agent or settings.user_agent,
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                    **auth_kwargs,
                )
                page = context.new_page()
                response = None
                landing = urls[0] if urls else ""
                needs_load = True
                last_html: str | None = None

                for index, plan in enumerate(plans):
                    if needs_load:
                        response = None
                        for url in urls:
                            try:
                                response = page.goto(
                                    url, timeout=self.timeout, wait_until="domcontentloaded"
                                )
                                landing = url
                                break
                            except Exception:  # noqa: BLE001, S112 - try the next mirror
                                continue
                        if response is None:
                            captures.extend(
                                self._failed(landing, "no configured url could be loaded")
                                for _ in plans[index:]
                            )
                            break
                        needs_load = False

                    # The probe's own flow and completion marker for this pass.
                    self.flow = plan.flow
                    self.wait_for = plan.wait_for
                    capture = self._capture_here(page, response, auth_warning)
                    captures.append(capture)
                    last_html = capture.html or last_html

                    # A dead session hits every probe the same way; stop at the first one.
                    if self._looks_logged_out(capture.html):
                        captures.extend(
                            self._failed(landing, "session expired") for _ in plans[index + 1 :]
                        )
                        break

                    if index + 1 < len(plans):
                        # A probe that navigates out of the panel leaves nothing to reset.
                        moved = plan.ends_navigation or (landing not in (page.url or ""))
                        # The modal belongs to the embedded panel, not to the page around
                        # it. Resetting the page found no modal, reported success, and left
                        # the old one open over the next method's click - which the site
                        # then answered with its "undefined" modal for every method.
                        needs_load = moved or not self._reset_panel(self._open_frame(page))

                self._refresh_session(context, page, last_html, None)
                context.close()
                browser.close()
        except Exception as exc:  # noqa: BLE001 - any browser failure is a failed run
            captures.extend(
                self._failed(urls[0] if urls else "", f"{type(exc).__name__}: {exc}")
                for _ in plans[len(captures) :]
            )
        return captures

    def _failed(self, url: str, error: str):
        return RawCapture(
            url=url,
            status_code=0,
            fetcher=self.name,
            fetched_at=datetime.now(UTC),
            error=error,
        )

    def _no_playwright(self, url: str):
        return self._failed(
            url,
            'playwright not installed; pip install -e ".[browser]" && playwright install chromium',
        )

    def _capture_here(self, page, response, auth_warning: str | None):
        """Walk the current probe's flow on an already-open page and capture the result."""
        target, frame_error = self._target(page)
        start_url = page.url
        flow_error = None if frame_error else self._walk(target, page)

        navigated = self._await_navigation(page, start_url)
        target = self._capture_target(page, navigated=navigated)
        declined = self._unavailable_hit
        settle_error = self._settle(
            target,
            skip_wait_for=frame_error is not None
            or flow_error is not None
            or declined is not None,
        )
        html, read_error = _read_html(target, page)
        return RawCapture(
            url=redact_url(target.url),
            status_code=response.status if response else 0,
            html=html,
            screenshot=page.screenshot(full_page=True) if self.screenshot else None,
            headers=dict(response.headers) if response else {},
            fetcher=self.name,
            fetched_at=datetime.now(UTC),
            flow_error=(frame_error or flow_error or settle_error or read_error or auth_warning),
            unavailable=declined,
        )

    def _await_navigation(self, page, start_url: str, budget_ms: int = 6000) -> bool:
        """Did the flow move the whole tab? Waits briefly, since navigation is async."""
        waited = 0
        while waited < budget_ms:
            if (page.url or "") != start_url:
                return True
            page.wait_for_timeout(300)
            waited += 300
        return False

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

    def _refresh_session(self, context, page, html: str | None, flow_error: str | None) -> None:
        """Write the session back after a fetch that was still signed in.

        Sites roll their session cookie as you browse. Restoring the same saved file for
        every probe and then discarding what the site handed back means the stored session
        only ever gets older, and expires on its own schedule no matter how often we
        collect. Saving it forward is what makes a run that works today make the next one
        work too, without anyone signing in again.

        Guarded hard in one direction: a logged-out capture must never be written over a
        good session. That would turn one expired session into a permanently broken one.
        """
        if not self.auth_state or not Path(self.auth_state).exists():
            return
        if flow_error == "LOGGED_OUT":
            return
        if self.logged_out_marker and html and self.logged_out_marker in html:
            return
        try:
            from ght.auth_login import _save_state

            _save_state(
                self.auth_state,
                context.storage_state(),
                page.evaluate("navigator.userAgent"),
                self._used_channel or self._session_channel,
            )
        except Exception:  # noqa: BLE001 - a session we could not re-save is not a failed fetch
            return

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
                start_url = page.url
                flow_error = None if frame_error else self._walk(target, page)

                # A step can navigate the whole tab (confirming a deposit hands off to the
                # provider's own checkout). That is asynchronous, so give it a moment to
                # start before deciding which document we are capturing.
                navigated = self._await_navigation(page, start_url)
                target = self._capture_target(page, navigated=navigated)
                # A method the site has switched off never renders the panel the probe
                # waits for, and waiting for it anyway would turn a two-second answer back
                # into a full timeout.
                declined = self._unavailable_hit
                settle_error = self._settle(
                    target,
                    skip_wait_for=frame_error is not None
                    or flow_error is not None
                    or declined is not None,
                )

                html, read_error = _read_html(target, page)
                capture = RawCapture(
                    url=redact_url(target.url),
                    status_code=response.status if response else 0,
                    html=html,
                    screenshot=page.screenshot(full_page=True) if self.screenshot else None,
                    headers=dict(response.headers) if response else {},
                    fetcher=self.name,
                    fetched_at=datetime.now(UTC),
                    flow_error=(
                        frame_error or flow_error or settle_error or read_error or auth_warning
                    ),
                    unavailable=declined,
                )
                self._refresh_session(context, page, html, capture.flow_error)
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
