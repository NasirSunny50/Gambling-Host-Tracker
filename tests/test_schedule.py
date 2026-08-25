"""Collecting on a timer.

The scheduler decides three things that matter: when the next collection is due, what to
do when one is already running, and whether a schedule survives the portal restarting.
All three are tested by driving ``tick()`` directly rather than by waiting on the clock,
so the suite stays fast and offline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ght.api.schedule import MIN_MINUTES, Scheduler


class FakeManager:
    """Stands in for the run manager: records what it was asked to collect."""

    def __init__(self, running: bool = False, accepts: bool = True):
        self.is_running = running
        self.accepts = accepts
        self.started: list[str] = []
        self.sources: list[str] = []

    def start(self, slug: str, source: str = "manual"):
        if not self.accepts:
            return False, f"a collection is already running ({slug})"
        self.started.append(slug)
        self.sources.append(source)
        return True, f"collection started for {slug}"


@pytest.fixture
def scheduler(tmp_path):
    def build(manager=None):
        return Scheduler(manager or FakeManager(), state_path=tmp_path / "schedule.json")

    return build


def _due_now(sched):
    """Pull the next run forward, standing in for the wall clock reaching it."""
    sched.state.next_due = datetime.now(UTC) - timedelta(seconds=1)


# ------------------------------------------------------------------ setting one


def test_starting_a_schedule_collects_straight_away(scheduler):
    """Someone who has just set this up should see it work, not be left wondering whether
    it will. The first collection goes now; the interval starts after it."""
    manager = FakeManager()
    sched = scheduler(manager)

    started, message = sched.start("1xbet-bd", 30)
    assert started
    assert "every 30 minutes" in message

    # Not yet: setting a schedule arranges for later. Firing one straight away made it
    # indistinguishable from the button beside it and started a fetch nobody asked for.
    assert sched.tick() is None
    assert manager.started == []
    assert 29 * 60 <= sched.seconds_until_next <= 30 * 60

    _due_now(sched)
    assert sched.tick() is not None
    assert manager.started == ["1xbet-bd"]


def test_the_next_one_is_an_interval_later(scheduler):
    manager = FakeManager()
    sched = scheduler(manager)
    sched.start("1xbet-bd", 30)
    _due_now(sched)
    sched.tick()

    # Nothing is due again yet, and nothing is collected.
    assert sched.tick() is None
    assert manager.started == ["1xbet-bd"]
    assert 29 * 60 <= sched.seconds_until_next <= 30 * 60


def test_a_scheduled_fetch_says_the_schedule_started_it(scheduler):
    """The portal shows a fetch in the lane it came from, so an operator who finds one
    already running can tell whether it is theirs or the timer firing unattended. That only
    works if the scheduler says so when it starts one."""
    manager = FakeManager()
    sched = scheduler(manager)
    sched.start("1xbet-bd", 30)
    _due_now(sched)
    sched.tick()

    assert manager.sources == ["schedule"]


def test_an_interval_too_short_to_be_sane_is_refused(scheduler):
    """A run takes over a minute and walks a live site. Below the floor the collector is
    never idle, which the numbers do not need and the site would notice."""
    sched = scheduler()
    started, message = sched.start("1xbet-bd", MIN_MINUTES - 1)
    assert not started
    assert str(MIN_MINUTES) in message
    assert sched.state.enabled is False


def test_a_day_is_the_longest_interval(scheduler):
    sched = scheduler()
    assert sched.start("1xbet-bd", 60 * 25)[0] is False


# ------------------------------------------------------------------ not racing


def test_a_tick_that_lands_on_a_running_collection_skips_it(scheduler):
    """Two collections at once would race on the same login session, and the next slot is
    minutes away. Skipping is right - but it has to say so, because a schedule that
    silently does nothing looks exactly like one that is switched off."""
    manager = FakeManager(running=True)
    sched = scheduler(manager)
    sched.start("1xbet-bd", 15)
    _due_now(sched)

    note = sched.tick()
    assert manager.started == []
    assert "still running" in note
    assert "still running" in sched.state.last_note
    # It does not retry immediately: the slot is skipped, not queued.
    assert sched.seconds_until_next > 60


def test_a_refused_start_is_reported_rather_than_swallowed(scheduler):
    manager = FakeManager(accepts=False)
    sched = scheduler(manager)
    sched.start("1xbet-bd", 15)
    _due_now(sched)

    sched.tick()
    assert "already running" in sched.state.last_note


def test_nothing_happens_while_the_schedule_is_off(scheduler):
    manager = FakeManager()
    sched = scheduler(manager)
    sched.start("1xbet-bd", 15)
    sched.stop()
    _due_now(sched)

    assert sched.tick() is None
    assert manager.started == []
    assert sched.seconds_until_next is None


# ------------------------------------------------------------------ across a restart


def _saved(path, **fields):
    """Write a schedule file the way a previous portal would have left it.

    Written directly rather than by driving a live Scheduler: that one owns a background
    thread which may reach the same file first, and a test should not be racing the thing
    it is testing.
    """
    state = {"enabled": True, "slug": "1xbet-bd", "minutes": 60,
             "next_due": datetime.now(UTC).isoformat(), "last_started": None, "last_note": ""}
    state.update(fields)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_a_schedule_outlives_the_portal(tmp_path):
    """A schedule that forgot itself on restart would be worse than none: the operator
    would believe collection was happening and find the gap days later."""
    path = _saved(tmp_path / "schedule.json", minutes=45)

    revived = Scheduler(FakeManager(), state_path=path)
    assert revived.state.enabled is True
    assert revived.state.slug == "1xbet-bd"
    assert revived.state.minutes == 45


def test_a_slot_missed_while_the_portal_was_down_is_not_caught_up_on(tmp_path):
    """Starting the portal must not fetch. Catching the missed slot up meant every start
    fetched immediately - the same surprise as a schedule that fetched the moment it was
    set, and on some sites a surprise that costs a deposit request."""
    missed = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    path = _saved(tmp_path / "schedule.json", minutes=60, next_due=missed)

    manager = FakeManager()
    revived = Scheduler(manager, state_path=path)
    revived.resume()

    assert revived.tick() is None
    assert manager.started == []
    # An interval from coming back up, not three hours ago and not an hour after that.
    assert 59 * 60 <= revived.seconds_until_next <= 60 * 60


def test_a_schedule_still_runs_after_a_restart(tmp_path):
    """Not catching up is not the same as not running: the next slot still comes."""
    missed = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    path = _saved(tmp_path / "schedule.json", minutes=60, next_due=missed)

    manager = FakeManager()
    revived = Scheduler(manager, state_path=path)
    revived.resume()
    _due_now(revived)

    assert revived.tick() is not None
    assert manager.started == ["1xbet-bd"]


def test_a_stopped_schedule_stays_stopped_across_a_restart(tmp_path):
    path = _saved(tmp_path / "schedule.json", enabled=False, next_due=None)

    manager = FakeManager()
    revived = Scheduler(manager, state_path=path)
    revived.resume()
    assert revived.state.enabled is False
    assert revived.tick() is None
    assert manager.started == []


# ------------------------------------------------------------------ saying it plainly


def test_an_interval_is_also_reported_as_runs_per_day(scheduler):
    """"Every 15 minutes" and "96 collections a day against a live site" are the same
    choice; only the second one makes it obvious."""
    sched = scheduler()
    sched.start("1xbet-bd", 15)
    assert sched.runs_per_day == 96

    sched.start("1xbet-bd", 360)
    assert sched.runs_per_day == 4


def test_setting_a_schedule_does_not_fetch_there_and_then(scheduler):
    """The two controls on the page do different things. One fetches now; the other says
    when to fetch. A schedule that also fetched now blurred them, and on a site whose
    payee is only reachable by confirming a deposit it raised a request nobody asked for."""
    manager = FakeManager()
    sched = scheduler(manager)

    sched.start("melbet-bd", 60)

    assert manager.started == []
    assert sched.state.enabled is True
    assert sched.seconds_until_next > 59 * 60
