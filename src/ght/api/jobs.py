"""Running a collection from the portal, without blocking the web server.

A collection drives a real browser and takes minutes, so it cannot run inside the request
that asks for it. It is launched as a detached subprocess — the same ``ght run`` the CLI
uses — and this module tracks the one that is in flight so the portal can show progress and
refuse to start a second on top of it.

Deliberately a single-run gate rather than a queue: two browser collections against the
same account at once would race on the login session, and there is no need to run more than
one at a time.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from ght.config import REPO_ROOT


@dataclass
class RunInfo:
    slug: str
    started_at: datetime
    finished_at: datetime | None = None
    returncode: int | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None


class RunManager:
    """Owns the at-most-one background collection process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: RunInfo | None = None
        self._proc: subprocess.Popen | None = None
        self._log_tail: list[str] = []

    @property
    def current(self) -> RunInfo | None:
        return self._current

    @property
    def is_running(self) -> bool:
        return self._current is not None and self._current.running

    def start(self, slug: str) -> tuple[bool, str]:
        """Launch ``ght run --site <slug>``. Returns (started, message)."""
        with self._lock:
            if self.is_running:
                return False, f"a collection is already running ({self._current.slug})"

            # Run the package as a module so it uses the same interpreter and installed
            # environment as the portal, rather than depending on a console script on PATH.
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "ght.cli", "run", "--site", slug],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._current = RunInfo(slug=slug, started_at=datetime.now(UTC))
            self._log_tail = []
            threading.Thread(target=self._watch, args=(self._proc, self._current), daemon=True).start()
            return True, f"collection started for {slug}"

    def _watch(self, proc: subprocess.Popen, info: RunInfo) -> None:
        """Drain output and record completion. Runs on its own thread."""
        if proc.stdout is not None:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._log_tail.append(line)
                    del self._log_tail[:-40]  # keep only the last 40 lines
        proc.wait()
        info.returncode = proc.returncode
        info.finished_at = datetime.now(UTC)

    @property
    def log_tail(self) -> list[str]:
        return list(self._log_tail)


# One manager for the process. The portal is single-process, so a module global is enough.
manager = RunManager()


@dataclass
class LoginInfo:
    slug: str
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool | None = None
    message: str = "running…"

    @property
    def running(self) -> bool:
        return self.finished_at is None


class LoginManager:
    """Runs headless credential logins in the background, one per site at a time.

    Kept separate from RunManager: a login is quick and site-scoped, and an operator may
    reasonably refresh one site's session while a collection of another is in flight.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_slug: dict[str, LoginInfo] = {}

    def status(self, slug: str) -> LoginInfo | None:
        return self._by_slug.get(slug)

    def is_running(self, slug: str) -> bool:
        info = self._by_slug.get(slug)
        return info is not None and info.running

    def start(self, slug: str) -> tuple[bool, str]:
        with self._lock:
            if self.is_running(slug):
                return False, f"a login for {slug} is already running"
            self._by_slug[slug] = LoginInfo(slug=slug, started_at=datetime.now(UTC))
        threading.Thread(target=self._run, args=(slug,), daemon=True).start()
        return True, f"login started for {slug}"

    def _run(self, slug: str) -> None:
        info = self._by_slug[slug]
        try:
            ok, message = _do_login(slug)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator, not crashed
            ok, message = False, f"{type(exc).__name__}: {exc}"
        info.ok = ok
        info.message = message
        info.finished_at = datetime.now(UTC)


def _do_login(slug: str) -> tuple[bool, str]:
    """Load the config and credentials, sign in, and record the outcome. No secrets logged."""
    from ght.auth_login import perform_login
    from ght.credentials import get_credentials
    from ght.crypto import SecretKeyInvalid, SecretKeyMissing
    from ght.db import session_scope
    from ght.models import Alert, Site
    from ght.sources import load_source

    config = load_source(slug)
    if config.login is None:
        return False, "this site has no login configured"

    with session_scope() as session:
        try:
            creds = get_credentials(session, slug)
        except (SecretKeyMissing, SecretKeyInvalid) as exc:
            return False, str(exc)
        if creds is None:
            return False, "no credentials stored for this site"
        username, password = creds

        result = perform_login(config, username, password)

        if not result.ok:
            site = session.scalar(select(Site).where(Site.slug == slug))
            atype = "login_challenge" if result.reason == "challenge" else "login_failed"
            session.add(
                Alert(
                    type=atype,
                    site_id=site.id if site else None,
                    # Reason and detail only — never the credentials.
                    payload={"reason": result.reason, "detail": result.detail},
                )
            )
        return result.ok, result.detail or result.reason


# One login manager for the process, alongside the collection manager.
login_manager = LoginManager()
