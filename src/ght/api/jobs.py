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

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def phases(self) -> list[dict]:
        """Every phase with its state, in order, for rendering as a checklist."""
        order = [name for name, _ in PHASES]
        current = order.index(self.phase) if self.phase in order else -1
        out = []
        for index, (name, label) in enumerate(PHASES):
            if not self.running and self.returncode == 0 or index < current:
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
                    continue
                self._log_tail.append(line)
                del self._log_tail[:-40]  # keep only the last 40 lines
        proc.wait()
        info.returncode = proc.returncode
        info.finished_at = datetime.now(UTC)
        info.message = "Finished" if proc.returncode == 0 else "Stopped before finishing"

    @property
    def log_tail(self) -> list[str]:
        return list(self._log_tail)


# One manager for the process. The portal is single-process, so a module global is enough.
manager = RunManager()
