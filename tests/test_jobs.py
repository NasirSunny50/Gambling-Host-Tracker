"""The portal's background-run manager.

The subprocess itself is never launched here: these cover the gate that keeps two browser
collections from racing on the same login session, which is the part with the bugs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ght.api.jobs import RunManager


class DummyProc:
    def __init__(self):
        self.returncode = 0
        self.stdout = iter(["status : ok\n", "\n", "account : bkash +8801...\n"])

    def wait(self):
        return 0


def test_a_run_reports_started_and_records_it(monkeypatch):
    manager = RunManager()
    monkeypatch.setattr("ght.api.jobs.subprocess.Popen", lambda *a, **k: DummyProc())
    started, message = manager.start("1xbet-bd")
    assert started is True
    assert "1xbet-bd" in message
    assert manager.current is not None
    assert manager.current.slug == "1xbet-bd"


def test_a_second_run_is_refused_while_one_is_active(monkeypatch):
    manager = RunManager()

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


def test_output_is_captured_as_a_bounded_tail(monkeypatch):
    manager = RunManager()

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


def test_a_reported_failure_survives_a_clean_exit_code(monkeypatch):
    """End to end through the watcher: the failure arrives as a progress line, the process
    then exits 0, and the manager must not overwrite it with "Finished"."""
    import json
    import time

    from ght.api.jobs import RunManager
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

    manager = RunManager()
    monkeypatch.setattr("ght.api.jobs.subprocess.Popen", lambda *a, **k: Failing())
    manager.start("1xbet-bd")
    for _ in range(50):
        if not manager.is_running:
            break
        time.sleep(0.02)
    assert manager.current.failed is True
    assert manager.current.message == "The saved session was not valid"
    assert [p["state"] for p in manager.current.phases] == ["stopped", "pending", "pending"]
