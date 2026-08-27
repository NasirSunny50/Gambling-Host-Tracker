"""One place that opens a Chromium browser, whichever build is available.

Playwright's bundled Chromium is often blocked by security software on Windows, and a
site's session is tied to the browser that made it — so collection and login share this
resolver. It prefers an explicitly configured browser (a Playwright channel name like
``msedge``, or a full path to any Chromium build such as Brave), falls back to the installed
browsers, and reports which it used so a saved session can be reopened in the same one.
"""

from __future__ import annotations

import os

from ght.config import settings

# Tried in order when nothing is configured: Playwright's own build, then installed ones.
DEFAULT_CHANNELS: tuple[str | None, ...] = (None, "msedge", "chrome")


def _looks_like_path(value: str | None) -> bool:
    """A channel name has no path separators; an executable location does."""
    return bool(value) and (os.sep in value or "/" in value or value.lower().endswith(".exe"))


def _launch_one(playwright, identity: str | None, headed: bool):
    if _looks_like_path(identity):
        return playwright.chromium.launch(headless=not headed, executable_path=identity)
    kwargs = {"channel": identity} if identity else {}
    return playwright.chromium.launch(headless=not headed, **kwargs)


def candidates(preferred: str | None) -> list[str | None]:
    """The browsers to try, most-specific first, de-duplicated."""
    seq: list[str | None] = []
    if settings.browser_path:
        seq.append(settings.browser_path)
    if preferred:
        seq.append(preferred)
    else:
        seq.extend(DEFAULT_CHANNELS)
    out: list[str | None] = []
    for c in seq:
        if c not in out:
            out.append(c)
    return out


def open_browser(playwright, *, headed: bool = False, preferred: str | None = None):
    """Return (browser, identity). Raises RuntimeError naming every attempt if none start."""
    failures = []
    for identity in candidates(preferred):
        try:
            browser = _launch_one(playwright, identity, headed)
            return browser, identity
        except Exception as exc:  # noqa: BLE001 - trying the next browser is the point
            label = identity or "bundled chromium"
            failures.append(f"{label} ({str(exc).splitlines()[0]})")
    raise RuntimeError("no usable browser: " + "; ".join(failures))


def describe_browser_failure(exc: BaseException) -> str:
    """What went wrong, in words a reader can act on.

    One failure needs translating. `sync_playwright()` builds its object only once the node
    driver connects and announces itself; when the driver dies first, `__enter__` reaches
    `playwright = self._playwright` with nothing there, so the run is recorded as

        AttributeError: 'PlaywrightContextManager' object has no attribute '_playwright'

    which names a private attribute of somebody else's library and says nothing about a
    browser. It is an environment failure an operator can actually fix - almost always
    browsers left behind by a run that was killed mid-flight, still holding sockets - but
    only if the run says which failure it was. Everything else is passed through as it is.
    """
    if isinstance(exc, AttributeError) and "_playwright" in str(exc):
        return (
            "the browser driver did not start. This is usually browsers left behind by a "
            "fetch that was killed mid-flight, still holding the machine's resources - "
            "clear any stray chrome/node processes and fetch again"
        )
    return f"{type(exc).__name__}: {exc}"
