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

from ght.config import REPO_ROOT
from ght.progress import PHASES, parse_line


@dataclass
class RunInfo:
    slug: str
    started_at: datetime
    finished_at: datetime | None = None
    returncode: int | None = None
    # Live state, updated from the subprocess as it works. The portal renders this as a
    # checklist so an operator can see which step is running - and, when it pauses, that it
    # is waiting for them to sign in rather than hung.
    phase: str = ""
    message: str = "Starting…"
    step: int | None = None
    total: int | None = None
    # The phase the run stopped at, when it reported one. A collection that ends badly
    # still exits 0 — it ran, and recorded a failed run — so the exit code says only that
    # the process finished. This is the one signal that says whether it worked.
    failed_phase: str | None = None
    # Whether the portal has already shown how this run ended. The outcome is news exactly
    # once; after that it is history, and history has its own table further down the page.
    seen: bool = False

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def failed(self) -> bool:
        return self.failed_phase is not None or (
            self.returncode is not None and self.returncode != 0
        )

    @property
    def phases(self) -> list[dict]:
        """Every phase with its state, in order, for rendering as a checklist."""
        order = [name for name, _ in PHASES]
        # A named failure wins over everything else: the phase it names is where the run
        # stopped, whatever the process did afterwards.
        if self.failed_phase in order:
            stopped_at = order.index(self.failed_phase)
            return [
                {
                    "name": name,
                    "label": label,
                    "state": "done"
                    if index < stopped_at
                    else ("stopped" if index == stopped_at else "pending"),
                }
                for index, (name, label) in enumerate(PHASES)
            ]

        current = order.index(self.phase) if self.phase in order else -1
        out = []
        for index, (name, label) in enumerate(PHASES):
            if (not self.running and self.returncode == 0) or index < current:
                state = "done"
            elif index == current:
                state = "active" if self.running else "stopped"
            else:
                state = "pending"
            out.append({"name": name, "label": label, "state": state})
        return out


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
                [sys.executable, "-m", "ght.cli", "run", "--site", slug, "--progress"],
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
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                update = parse_line(line)
                if update is not None:
                    # A progress line drives the checklist and is not shown as raw output.
                    info.phase = update.phase
                    info.message = update.message
                    info.step = update.step
                    info.total = update.total
                    if not update.ok:
                        info.failed_phase = update.phase
                    continue
                self._log_tail.append(line)
                del self._log_tail[:-40]  # keep only the last 40 lines
        proc.wait()
        info.returncode = proc.returncode
        info.finished_at = datetime.now(UTC)
        if info.failed_phase is not None:
            # Keep what the run said went wrong. Replacing it with "Finished" because the
            # process exited cleanly is how a failed collection came to look successful.
            info.message = info.message or "Stopped before finishing"
        else:
            info.message = "Finished" if proc.returncode == 0 else "Stopped before finishing"

    def take_finished(self) -> RunInfo | None:
        """The run that has just ended, reported once and then not again.

        A finished run is an announcement, not a state: the operator needs to see how it
        went the moment it stops, and then get their page back. Reloading is how someone
        says they have read it, so the second render is the one that stops showing it.
        The run itself is not forgotten — it is in the history table, permanently.
        """
        with self._lock:
            info = self._current
            if info is None or info.running or info.seen:
                return None
            info.seen = True
            return info

    @property
    def log_tail(self) -> list[str]:
        return list(self._log_tail)


# One manager for the process. The portal is single-process, so a module global is enough.
manager = RunManager()
