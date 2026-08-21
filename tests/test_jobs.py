"""The portal's background-run manager.

The subprocess itself is never launched here: these cover the gate that keeps two browser
collections from racing on the same login session, which is the part with the bugs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ght.api.jobs import RunManager


class DummyProc:
    def __init__(self):
        self.returncode = 0
        self.stdout = iter(["status : ok\n", "\n", "account : bkash +8801...\n"])

    def wait(self):
        return 0


@pytest.fixture
def manager(tmp_path):
    """A manager whose cross-process lock lives in the test's own directory.

    Without this the lock is the repo's real one: a test that leaves a run "in flight"
    holds it under this very pid, and every test after it is refused.
    """
    return RunManager(lock_path=tmp_path / "run.lock")


def test_a_run_reports_started_and_records_it(monkeypatch, manager):
    monkeypatch.setattr("ght.api.jobs.subprocess.Popen", lambda *a, **k: DummyProc())
    started, message = manager.start("1xbet-bd")
    assert started is True
    assert "1xbet-bd" in message
    assert manager.current is not None
    assert manager.current.slug == "1xbet-bd"


def test_a_second_run_is_refused_while_one_is_active(monkeypatch, manager):

    class NeverEnds:
        returncode = None
        stdout = iter([])

        def wait(self):
            import time

            time.sleep(0.2)
            return 0

    monkeypatch.setattr("ght.api.jobs.subprocess.Popen", lambda *a, **k: NeverEnds())
    assert manager.start("1xbet-bd")[0] is True
    # The watcher thread has not seen the process end yet, so a second start is refused.
    started, message = manager.start("demo-site")
    assert started is False
    assert "already running" in message


def test_output_is_captured_as_a_bounded_tail(monkeypatch, manager):

    class Chatty:
        returncode = 0
        stdout = iter(f"line {i}\n" for i in range(100))

        def wait(self):
            return 0

    monkeypatch.setattr("ght.api.jobs.subprocess.Popen", lambda *a, **k: Chatty())
    manager.start("1xbet-bd")
    # Give the watcher thread a moment to drain the iterator.
    import time

    for _ in range(50):
        if not manager.is_running:
            break
        time.sleep(0.02)
    tail = manager.log_tail
    assert len(tail) <= 40
    assert tail[-1] == "line 99"


def test_run_info_running_flag_flips_on_finish():
    from ght.api.jobs import RunInfo

    info = RunInfo(slug="x", started_at=datetime.now(UTC))
    assert info.running is True
    info.finished_at = datetime.now(UTC)
    assert info.running is False


# --------------------------------------------------------- what the checklist claims


def _info(**kw):
    from ght.api.jobs import RunInfo

    return RunInfo(slug="1xbet-bd", started_at=datetime.now(UTC), **kw)


def test_a_failed_run_does_not_tick_every_phase_green():
    """The bug this pins: `ght run` records a failed collection and still exits 0, so a
    checklist keyed on the exit code showed sign-in, collection and storage all "done"
    for a run whose error said the session was never valid."""
    info = _info(phase="signin", failed_phase="signin", finished_at=datetime.now(UTC), returncode=0)
    states = [p["state"] for p in info.phases]
    assert states == ["stopped", "pending", "pending"]
    assert info.failed is True


def test_a_run_that_failed_later_keeps_the_phases_it_did_finish():
    info = _info(phase="collect", failed_phase="collect", finished_at=datetime.now(UTC), returncode=0)
    assert [p["state"] for p in info.phases] == ["done", "stopped", "pending"]


def test_a_clean_run_still_reads_as_done_throughout():
    info = _info(phase="store", finished_at=datetime.now(UTC), returncode=0)
    assert [p["state"] for p in info.phases] == ["done", "done", "done"]
    assert info.failed is False


def test_a_reported_failure_survives_a_clean_exit_code(monkeypatch, manager):
    """End to end through the watcher: the failure arrives as a progress line, the process
    then exits 0, and the manager must not overwrite it with "Finished"."""
    import json
    import time

    from ght.progress import MARKER, Update

    class Failing:
        returncode = 0
        stdout = iter(
            [
                MARKER + json.dumps(Update("signin", "Checking the site sign-in").as_dict()),
                MARKER
                + json.dumps(
                    Update("signin", "The saved session was not valid", ok=False).as_dict()
                ),
            ]
        )

        def wait(self):
            return 0

    monkeypatch.setattr("ght.api.jobs.subprocess.Popen", lambda *a, **k: Failing())
    manager.start("1xbet-bd")
    for _ in range(50):
        if not manager.is_running:
            break
        time.sleep(0.02)
    assert manager.current.failed is True
    assert manager.current.message == "The saved session was not valid"
    assert [p["state"] for p in manager.current.phases] == ["stopped", "pending", "pending"]


# ------------------------------------------------- one collection on the machine, not per portal


def test_a_collection_started_by_another_portal_blocks_this_one(tmp_path):
    """Observed for real: two portals, both resuming the same saved schedule, two
    collections walking one login session at once. The gate has to hold across processes,
    not just within one."""
    import json
    import os
    from datetime import UTC, datetime

    from ght.api.jobs import RunManager

    lock = tmp_path / "run.lock"
    lock.write_text(
        json.dumps({"pid": os.getpid(), "slug": "someone-else", "started_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )

    manager = RunManager(lock_path=lock)
    started, message = manager.start("1xbet-bd")
    assert started is False
    assert "another collection" in message
    assert "someone-else" in message


def test_a_lock_left_behind_by_a_dead_process_does_not_block_forever(tmp_path):
    """A crash must not stop the tool collecting until someone finds a file they have
    never heard of."""
    import json
    from datetime import UTC, datetime

    from ght.api.jobs import _lock_holder

    lock = tmp_path / "run.lock"
    # A pid that cannot be running: process 0 is never a real user process.
    lock.write_text(
        json.dumps({"pid": 0, "slug": "crashed", "started_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )

    assert _lock_holder(lock) is None
    assert not lock.exists()


def test_a_lock_older_than_any_real_collection_is_stale(tmp_path):
    import json
    import os
    from datetime import UTC, datetime, timedelta

    from ght.api.jobs import _lock_holder

    lock = tmp_path / "run.lock"
    long_ago = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
    lock.write_text(
        json.dumps({"pid": os.getpid(), "slug": "forgotten", "started_at": long_ago}),
        encoding="utf-8",
    )

    assert _lock_holder(lock) is None


def test_an_unreadable_lock_is_not_treated_as_held(tmp_path):
    from ght.api.jobs import _lock_holder

    lock = tmp_path / "run.lock"
    lock.write_text("not json at all", encoding="utf-8")
    assert _lock_holder(lock) is None


def test_the_collector_takes_the_lock_and_gives_it_back(tmp_path):
    """The collector holds it, not the portal: a run started from a terminal has to be
    visible to a schedule firing on its own, and only the collector knows about both."""
    from ght.api.jobs import claim_run_lock, release_run_lock

    lock = tmp_path / "run.lock"
    assert claim_run_lock("1xbet-bd", lock) is None
    assert lock.exists()

    # A second collection, from anywhere, is told who has it.
    held = claim_run_lock("1xbet-bd", lock)
    assert held is not None and held["slug"] == "1xbet-bd"

    release_run_lock(lock)
    assert claim_run_lock("1xbet-bd", lock) is None
