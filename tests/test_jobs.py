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
