"""Choosing which browser to open, for both collection and login."""

from __future__ import annotations

from ght import browserlaunch


class FakeChromium:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.calls = []

    def launch(self, headless=True, channel=None, executable_path=None):
        identity = executable_path or channel or "bundled"
        self.calls.append(identity)
        if identity in self.blocked:
            raise RuntimeError(f"{identity} blocked")
        return f"browser:{identity}"


class FakePW:
    def __init__(self, chromium):
        self.chromium = chromium


def test_a_configured_path_is_preferred(monkeypatch):
    monkeypatch.setattr(browserlaunch.settings, "browser_path", r"C:\x\brave.exe")
    chromium = FakeChromium()
    browser, identity = browserlaunch.open_browser(FakePW(chromium), preferred=None)
    assert identity == r"C:\x\brave.exe"
    assert browser == r"browser:C:\x\brave.exe"


def test_a_path_uses_executable_path_not_channel(monkeypatch):
    monkeypatch.setattr(browserlaunch.settings, "browser_path", "")
    chromium = FakeChromium()
    browserlaunch.open_browser(FakePW(chromium), preferred="/opt/brave")
    assert chromium.calls == ["/opt/brave"]  # launched by path, resolved as executable


def test_channel_names_are_not_treated_as_paths(monkeypatch):
    monkeypatch.setattr(browserlaunch.settings, "browser_path", "")
    chromium = FakeChromium()
    browserlaunch.open_browser(FakePW(chromium), preferred="msedge")
    assert chromium.calls == ["msedge"]


def test_falls_back_through_the_default_chain(monkeypatch):
    monkeypatch.setattr(browserlaunch.settings, "browser_path", "")
    chromium = FakeChromium(blocked={"bundled"})
    _browser, identity = browserlaunch.open_browser(FakePW(chromium), preferred=None)
    assert identity == "msedge"
    assert chromium.calls == ["bundled", "msedge"]


def test_names_every_attempt_when_none_start(monkeypatch):
    monkeypatch.setattr(browserlaunch.settings, "browser_path", "")
    chromium = FakeChromium(blocked={"bundled", "msedge", "chrome"})
    import pytest

    with pytest.raises(RuntimeError) as exc:
        browserlaunch.open_browser(FakePW(chromium), preferred=None)
    assert "msedge" in str(exc.value) and "chrome" in str(exc.value)
