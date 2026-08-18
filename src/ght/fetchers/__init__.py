"""Fetchers turn a URL into a RawCapture. Which one a site uses is set in its YAML."""

from __future__ import annotations

from ght.fetchers.base import Fetcher, looks_blocked
from ght.fetchers.http import HttpFetcher

__all__ = ["Fetcher", "HttpFetcher", "get_fetcher", "looks_blocked"]


def get_fetcher(name: str, **kwargs) -> Fetcher:
    """Return a fetcher by the name used in sources/<slug>.yaml.

    The browser fetcher is imported lazily so Playwright stays an optional dependency.
    """
    if name == "browser":
        from ght.fetchers.browser import BrowserFetcher

        return BrowserFetcher(**kwargs)
    return HttpFetcher(**kwargs)
