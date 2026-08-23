"""Collecting on a timer, without anyone at the keyboard.

The portal already runs one collection at a time on request. This adds the other half: a
site can be put on an interval and left alone, which is what the data actually needs — the
numbers rotate two or three times a day and a person cannot be there for every rotation.

Deliberately small. It is one interval for one site, held in the portal process, with a
thread that wakes up, checks whether the next run is due, and asks the same RunManager the
button asks. There is no queue, no catch-up and no second worker: a collection drives a
real browser against a live site, and the failure mode of a clever scheduler here is two
of them racing on one login session.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ght.config import REPO_ROOT

# Where the schedule is remembered. A schedule that quietly forgot itself when the portal
# restarted would be worse than no schedule at all: the operator would believe collection
# was happening and find a gap in the data days later.
STATE_PATH = REPO_ROOT / "data" / "schedule.json"

# A run takes well over a minute, and every one signs in and walks a live site. Below this
# the collector would essentially never be idle, which is both pointless — the numbers do
# not rotate that fast — and the surest way to get the session blocked.
MIN_MINUTES = 5
MAX_MINUTES = 24 * 60


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ScheduleState:
    enabled: bool = False
    slug: str = ""
    minutes: int = 0
    next_due: datetime | None = None
    last_started: datetime | None = None
    # Why the last tick did nothing, when it did nothing. A schedule that silently skips
    # is indistinguishable from one that is not running.
    last_note: str = ""

    def as_json(self) -> dict:
        return {
            "enabled": self.enabled,
            "slug": self.slug,
            "minutes": self.minutes,
            "next_due": self.next_due.isoformat() if self.next_due else None,
            "last_started": self.last_started.isoformat() if self.last_started else None,
            "last_note": self.last_note,
        }

    @classmethod
    def from_json(cls, data: dict) -> ScheduleState:
        def when(value):
            return datetime.fromisoformat(value) if value else None

        return cls(
            enabled=bool(data.get("enabled")),
            slug=str(data.get("slug") or ""),
            minutes=int(data.get("minutes") or 0),
            next_due=when(data.get("next_due")),
            last_started=when(data.get("last_started")),
            last_note=str(data.get("last_note") or ""),
        )


class Scheduler:
    """Starts one site's collection on an interval."""

    # How often the thread wakes to look at the clock. Short enough that a schedule
    # started now is honoured close to the minute, long enough to cost nothing.
    TICK_SECONDS = 5

    def __init__(self, manager, state_path: Path = STATE_PATH) -> None:
        self._manager = manager
        self._path = state_path
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._load()

    # ---------------------------------------------------------------- state on disk

    def _load(self) -> ScheduleState:
        try:
            return ScheduleState.from_json(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return ScheduleState()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._state.as_json(), indent=2), encoding="utf-8"
            )
        except OSError:
            # A schedule that cannot be written still works until restart; losing it is
            # not worth failing the request the operator just made.
            pass

    # ---------------------------------------------------------------- reading

    @property
    def state(self) -> ScheduleState:
        return self._state

    @property
    def seconds_until_next(self) -> int | None:
        state = self._state
        if not state.enabled or state.next_due is None:
            return None
        return max(0, int((state.next_due - _now()).total_seconds()))

    @property
    def runs_per_day(self) -> int:
        """How many collections a day this interval works out to.

        Shown rather than left for the operator to calculate: "every 15 minutes" and
        "ninety-six runs a day against a live site" are the same fact, and only one of
        them makes the choice obvious.
        """
        return int((24 * 60) / self._state.minutes) if self._state.minutes else 0

    # ---------------------------------------------------------------- control

    def start(self, slug: str, minutes: int) -> tuple[bool, str]:
        """Put a site on an interval. Returns (started, message)."""
        if minutes < MIN_MINUTES:
            return False, f"the shortest interval is {MIN_MINUTES} minutes"
        if minutes > MAX_MINUTES:
            return False, "the longest interval is 24 hours"

        with self._lock:
            self._state = ScheduleState(
                enabled=True,
                slug=slug,
                minutes=minutes,
                # The first fetch is one interval away, not immediate. Setting a schedule
                # is arranging for later; firing one straight away made it indistinguishable
                # from the button beside it, and started a fetch nobody had asked for -
                # including, on some sites, a deposit request.
                next_due=_now() + timedelta(minutes=minutes),
                last_started=self._state.last_started,
                last_note="",
            )
            self._save()
        self._ensure_thread()
        self._wake.set()
        return True, f"collecting {slug} every {minutes} minutes"

    def stop(self) -> None:
        with self._lock:
            self._state.enabled = False
            self._state.next_due = None
            self._state.last_note = ""
            self._save()
        self._wake.set()

    def resume(self) -> None:
        """Pick a saved schedule back up after a restart.

        A slot missed while the portal was down is not caught up on. Starting the portal
        would otherwise fetch immediately, every time, which is the same surprise as a
        schedule that fetched the moment it was set - and on a site whose payee is only
        reachable by confirming a deposit, a surprise that costs a deposit request. The
        next fetch is one interval from coming back up.
        """
        if self._state.enabled:
            if self._state.next_due is None or self._state.next_due < _now():
                self._state.next_due = _now() + timedelta(minutes=self._state.minutes)
                self._save()
            self._ensure_thread()

    # ---------------------------------------------------------------- the loop

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ght-scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while True:
            self._wake.wait(self.TICK_SECONDS)
            self._wake.clear()
            if not self._state.enabled:
                continue
            self.tick()

    def tick(self) -> str | None:
        """One look at the clock. Returns what it did, or None if nothing was due.

        Separate from the loop so it can be tested without waiting on wall-clock time.
        """
        with self._lock:
            state = self._state
            if not state.enabled or state.next_due is None or _now() < state.next_due:
                return None

            # A collection already in flight is not something to queue behind. Two of them
            # would race on the same login session, and the next slot is minutes away.
            if self._manager.is_running:
                state.last_note = "skipped — the previous collection was still running"
                state.next_due = _now() + timedelta(minutes=state.minutes)
                self._save()
                return state.last_note

            started, message = self._manager.start(state.slug)
            state.last_started = _now()
            state.last_note = message if not started else ""
            state.next_due = _now() + timedelta(minutes=state.minutes)
            self._save()
            return message
