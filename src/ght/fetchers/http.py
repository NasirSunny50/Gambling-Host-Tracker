"""Plain HTTP fetcher.

The default for sites that render their deposit details server-side. One request per run,
with a short backoff on transient failures — this is monitoring-level traffic, not a crawl.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx

from ght.config import settings
from ght.types import RawCapture

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpFetcher:
    name = "http"

    def __init__(self, timeout: int | None = None, max_retries: int | None = None) -> None:
        self.timeout = timeout if timeout is not None else settings.request_timeout
        self.max_retries = max_retries if max_retries is not None else settings.max_retries

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
        }

    def fetch(self, url: str) -> RawCapture:
        last_error: str | None = None
        status_code = 0
        headers: dict[str, str] = {}

        for attempt in range(self.max_retries):
            try:
                with httpx.Client(
                    timeout=self.timeout, follow_redirects=True, headers=self._headers()
                ) as client:
                    response = client.get(url)
                status_code = response.status_code
                headers = dict(response.headers)

                if status_code in RETRYABLE_STATUS and attempt < self.max_retries - 1:
                    last_error = f"HTTP {status_code}"
                    time.sleep(2**attempt)
                    continue

                is_json = "json" in response.headers.get("content-type", "").lower()
                return RawCapture(
                    url=str(response.url),
                    status_code=status_code,
                    html=None if is_json else response.text,
                    json_body=response.text if is_json else None,
                    headers=headers,
                    fetcher=self.name,
                    fetched_at=datetime.now(UTC),
                )
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)

        return RawCapture(
            url=url,
            status_code=status_code,
            headers=headers,
            fetcher=self.name,
            fetched_at=datetime.now(UTC),
            error=last_error or "fetch failed",
        )
