"""Multi-step deposit flows and the payee name that comes with them.

Some sites print nothing on the deposit page: you pick a method, confirm, and the account
only appears on the payment provider's page. These tests cover the two pieces that makes
possible — the configured click flow, and the holder selector that captures the merchant
name printed next to the number.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ght.extractors.base import extract
from ght.fetchers.browser import BrowserFetcher, redact_url
from ght.sources import Block, SourceConfig, SourceUrl, Step, scan_sources
from ght.types import CONFIDENCE_HIGH

FIXTURES = Path(__file__).parent / "fixtures" / "html"


@pytest.fixture(scope="module")
def psp_html():
    return (FIXTURES / "psp_merchant_page.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def psp_config():
    return SourceConfig(
        slug="psp-fixture",
        name="PSP redirect (fixture)",
        fetcher="browser",
        urls=[SourceUrl(url="https://psp-fixture.invalid/pay")],
        blocks=[
            Block(
                channel="nagad",
                container=".psp-panel",
                value=".account-number",
                holder=".merchant-name",
            )
        ],
        ignore_numbers=["01500000000"],
    )


# --------------------------------------------------------------------- holder selector


def test_holder_selector_captures_the_merchant_name(psp_html, psp_config):
    result = extract(psp_html, psp_config)
    accounts = [a for a in result.accounts if a.confidence >= CONFIDENCE_HIGH]
    assert len(accounts) == 1
    account = accounts[0]
    assert account.channel == "nagad"
    assert account.account_number == "+8801812345678"
    # The name is a bare div with no "A/C Name:" label, so only the selector can find it.
    assert account.holder_name == "RIYA FASHION"


def test_holder_is_optional(psp_html, psp_config):
    without_holder = psp_config.model_copy(
        update={
            "blocks": [
                Block(channel="nagad", container=".psp-panel", value=".account-number")
            ]
        }
    )
    account = extract(psp_html, without_holder).accounts[0]
    assert account.holder_name is None


def test_missing_holder_element_does_not_break_extraction(psp_html, psp_config):
    stale = psp_config.model_copy(
        update={
            "blocks": [
                Block(
                    channel="nagad",
                    container=".psp-panel",
                    value=".account-number",
                    holder=".renamed-in-a-redesign",
                )
            ]
        }
    )
    account = extract(psp_html, stale).accounts[0]
    assert account.account_number == "+8801812345678"
    assert account.holder_name is None


# ------------------------------------------------------------------------ flow config


def test_flow_requires_the_browser_fetcher():
    with pytest.raises(ValidationError, match="flow requires fetcher: browser"):
        SourceConfig(
            slug="x",
            name="x",
            fetcher="http",
            flow=[Step(click="#deposit_button")],
        )


def test_flow_is_allowed_on_a_browser_source():
    config = SourceConfig(
        slug="x", name="x", fetcher="browser", flow=[Step(click="#deposit_button")]
    )
    assert config.flow[0].click == "#deposit_button"
    assert config.flow[0].optional is False


# -------------------------------------------------------------------------- flow walk


class FakePage:
    """Minimal stand-in for a Playwright page, so the walk is testable without a browser."""

    def __init__(self, failing: str | None = None):
        self.failing = failing
        self.clicked: list[str] = []
        self.waited: list[str] = []

    def wait_for_selector(self, selector, timeout=None):
        self.waited.append(selector)
        if selector == self.failing:
            raise TimeoutError(f"no element matching {selector}")

    def click(self, selector, timeout=None):
        self.clicked.append(selector)

    def wait_for_load_state(self, state, timeout=None):
        pass


def test_walk_clicks_every_step_in_order():
    fetcher = BrowserFetcher(
        flow=[Step(click=".payment-cell", wait_for="#deposit_button"), Step(click="#deposit_button")]
    )
    page = FakePage()
    assert fetcher._walk(page) is None
    assert page.clicked == [".payment-cell", "#deposit_button"]


def test_walk_reports_which_step_broke():
    fetcher = BrowserFetcher(flow=[Step(click=".gone"), Step(click="#deposit_button")])
    page = FakePage(failing=".gone")
    error = fetcher._walk(page)
    assert error is not None
    assert "step 1" in error and ".gone" in error
    # It stops at the break rather than clicking on into an unknown page state.
    assert page.clicked == []


def test_optional_step_that_is_absent_is_skipped():
    fetcher = BrowserFetcher(
        flow=[Step(click=".interstitial", optional=True), Step(click="#deposit_button")]
    )
    page = FakePage(failing=".interstitial")
    assert fetcher._walk(page) is None
    assert page.clicked == ["#deposit_button"]


def test_a_bare_playwright_export_is_still_accepted(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    fetcher = BrowserFetcher(auth_state=str(state))
    kwargs, warning = fetcher._auth_kwargs()
    assert kwargs == {"storage_state": {"cookies": [], "origins": []}}
    assert warning is None
    assert fetcher._session_user_agent is None


def test_session_carries_the_browser_identity_that_made_it(tmp_path):
    """Replaying cookies under a different UA invalidates Cloudflare's clearance cookie."""
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "storage_state": {"cookies": [{"name": "SESSION"}], "origins": []},
                "user_agent": "Mozilla/5.0 Edg/151.0.0.0",
                "channel": "msedge",
            }
        ),
        encoding="utf-8",
    )
    fetcher = BrowserFetcher(auth_state=str(state))
    kwargs, warning = fetcher._auth_kwargs()
    assert warning is None
    assert kwargs["storage_state"]["cookies"] == [{"name": "SESSION"}]
    assert fetcher._session_user_agent == "Mozilla/5.0 Edg/151.0.0.0"
    assert fetcher._session_channel == "msedge"


def test_the_session_browser_is_preferred_when_launching():
    """Launching a different browser than the session was made in defeats the point."""
    state = {"storage_state": {}, "channel": "msedge"}
    fetcher = BrowserFetcher()
    fetcher._session_channel = state["channel"]
    chromium = FakeChromium()
    fetcher._launch(FakePlaywright(chromium))
    assert chromium.attempts == ["msedge"]


def test_an_explicit_channel_overrides_the_session_one():
    fetcher = BrowserFetcher(channel="chrome")
    fetcher._session_channel = "msedge"
    chromium = FakeChromium()
    fetcher._launch(FakePlaywright(chromium))
    assert chromium.attempts == ["chrome"]


def test_missing_auth_state_warns_but_still_collects(tmp_path):
    """A logged-out capture is a real observation; it must not fail the whole run."""
    kwargs, warning = BrowserFetcher(auth_state=str(tmp_path / "nope.json"))._auth_kwargs()
    assert kwargs == {}
    assert "not found" in warning


def test_unreadable_auth_state_is_named_rather_than_crashing(tmp_path):
    """Handing this straight to Playwright surfaced only a bare JSONDecodeError."""
    state = tmp_path / "state.json"
    state.write_text("not json at all", encoding="utf-8")
    kwargs, warning = BrowserFetcher(auth_state=str(state))._auth_kwargs()
    assert kwargs == {}
    assert "not readable session JSON" in warning
    assert str(state) in warning


# ----------------------------------------------------------------------------- settle


def test_a_stale_wait_for_is_reported_not_raised():
    """The capture must survive it — the page we landed on is what explains the break."""
    fetcher = BrowserFetcher(wait_for=".merchant-name")
    error = fetcher._settle(FakePage(failing=".merchant-name"))
    assert error is not None
    assert ".merchant-name" in error


def test_settle_skips_wait_for_once_the_flow_has_broken():
    """A selector from the page we never reached would only burn the timeout twice."""
    fetcher = BrowserFetcher(wait_for=".merchant-name")
    page = FakePage(failing=".merchant-name")
    assert fetcher._settle(page, skip_wait_for=True) is None
    assert ".merchant-name" not in page.waited


def test_flow_error_wins_over_settle_error():
    """Both fire together when a flow breaks; the flow error is the one that explains it."""
    fetcher = BrowserFetcher(
        flow=[Step(click=".gone")],
        wait_for=".merchant-name",
    )
    page = FakePage(failing=".gone")
    flow_error = fetcher._walk(page)
    settle_error = fetcher._settle(page, skip_wait_for=flow_error is not None)
    assert (flow_error or settle_error).startswith("flow step 1")


# ------------------------------------------------------------------- holder scoping


def test_holder_without_a_container_is_rejected():
    """Unscoped, it searched the whole page and pinned one name onto every account."""
    with pytest.raises(ValidationError, match="needs a container"):
        Block(channel="nagad", value=".account-number", holder=".merchant-name")


def test_holder_does_not_leak_between_sibling_blocks():
    html = """
    <body>
      <div class="psp-panel">
        <div class="merchant-name">RIYA FASHION</div>
        <div class="account-number">01812345678</div>
      </div>
      <div class="psp-panel">
        <div class="merchant-name">HASAN TRADERS</div>
        <div class="account-number">01912345679</div>
      </div>
    </body>"""
    config = SourceConfig(
        slug="two-panels",
        name="two panels",
        blocks=[
            Block(
                channel="nagad",
                container=".psp-panel",
                value=".account-number",
                holder=".merchant-name",
            )
        ],
    )
    holders = {a.account_number: a.holder_name for a in extract(html, config).accounts}
    assert holders == {
        "+8801812345678": "RIYA FASHION",
        "+8801912345679": "HASAN TRADERS",
    }


# ----------------------------------------------------------------- source scanning


def test_one_broken_config_does_not_hide_the_others(tmp_path):
    """A typo in one YAML used to abort the listing for every site."""
    (tmp_path / "good.yaml").write_text(
        "slug: good\nname: Good Site\n", encoding="utf-8"
    )
    (tmp_path / "bad.yaml").write_text(
        'slug: bad\nname: Bad Site\nblocks:\n  - channel: nagad\n    value: ".v"\n    holder: ".h"\n',
        encoding="utf-8",
    )
    configs, broken = scan_sources(tmp_path)
    assert [c.slug for c in configs] == ["good"]
    assert len(broken) == 1
    assert broken[0].path.name == "bad.yaml"
    assert "needs a container" in broken[0].error


def test_broken_config_error_is_a_single_readable_line(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "slug: bad\nname: Bad\nfetcher: carrier-pigeon\n", encoding="utf-8"
    )
    _, broken = scan_sources(tmp_path)
    assert "\n" not in broken[0].error
    assert "carrier-pigeon" in broken[0].error


def test_underscore_prefixed_files_are_skipped(tmp_path):
    (tmp_path / "_wip.yaml").write_text("nonsense: [", encoding="utf-8")
    configs, broken = scan_sources(tmp_path)
    assert configs == [] and broken == []


# ------------------------------------------------------------------ browser launching


class FakeChromium:
    """Records launch attempts and fails the channels named in ``blocked``."""

    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.attempts: list[str | None] = []

    def launch(self, headless=True, channel=None):
        self.attempts.append(channel)
        # None stands for Playwright's own bundled build.
        if (channel or "bundled") in self.blocked:
            raise RuntimeError(f"BrowserType.launch: {channel or 'bundled'} unavailable")
        return f"browser:{channel or 'bundled'}"


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


def test_bundled_chromium_is_preferred():
    chromium = FakeChromium()
    assert BrowserFetcher()._launch(FakePlaywright(chromium)) == "browser:bundled"
    assert chromium.attempts == [None]


def test_a_quarantined_bundled_browser_falls_back_to_an_installed_one():
    """Security software removing Playwright's Chromium must not end the collection."""
    chromium = FakeChromium(blocked={"bundled"})
    assert BrowserFetcher()._launch(FakePlaywright(chromium)) == "browser:msedge"
    assert chromium.attempts == [None, "msedge"]


def test_pinned_channel_is_not_second_guessed():
    chromium = FakeChromium()
    fetcher = BrowserFetcher(channel="msedge")
    assert fetcher._launch(FakePlaywright(chromium)) == "browser:msedge"
    assert chromium.attempts == ["msedge"]


def test_every_tried_browser_is_named_when_none_start():
    chromium = FakeChromium(blocked={"bundled", "msedge", "chrome"})
    with pytest.raises(RuntimeError) as exc:
        BrowserFetcher()._launch(FakePlaywright(chromium))
    message = str(exc.value)
    assert "bundled chromium" in message and "msedge" in message and "chrome" in message


# ------------------------------------------------------------------------- url safety


def test_session_tokens_are_stripped_from_the_stored_url():
    """The captured URL lands in the run record and in AML exports."""
    url = (
        "https://bd.1xbet.com/paysystems/deposit/?host=https%3A%2F%2Fbd.1xbet.com%2F"
        "&lng=en&h_token=eyJhbGciOiJFUzI1NiJ9.payload.signature&sub_id=1772948457"
    )
    redacted = redact_url(url)
    assert "eyJhbGciOiJFUzI1NiJ9" not in redacted
    assert "h_token=REDACTED" in redacted
    # Everything that is not a credential still has to survive, or the URL stops
    # identifying which page was captured.
    assert "lng=en" in redacted
    assert "sub_id=1772948457" in redacted


def test_urls_without_a_query_are_untouched():
    assert redact_url("https://bd.1xbet.com/en/office/recharge") == (
        "https://bd.1xbet.com/en/office/recharge"
    )


# ------------------------------------------------------------- expired-session detection


class LoggedOutPage:
    """A page that never yields the payment frame — the logged-out case."""

    url = "https://bd.1xbet.com/en/office/recharge"

    def __init__(self, html):
        self._html = html
        self.frames = [self]  # only itself; no /paysystems frame ever appears
        self.waited = 0

    def content(self):
        return self._html

    def wait_for_timeout(self, ms):
        self.waited += ms


def test_target_bails_immediately_when_logged_out():
    """A dead session must not burn the whole timeout waiting for a frame that won't come."""
    fetcher = BrowserFetcher(frame="/paysystems/deposit", logged_out_marker="registration-layout")
    page = LoggedOutPage("<div class='registration-layout-widget'>sign up</div>")
    _target, error = fetcher._target(page)
    assert error == "LOGGED_OUT"
    # It bailed on the first content check, not after polling out the timeout.
    assert page.waited == 0


def test_target_still_finds_the_frame_when_logged_in():
    class Frame:
        url = "https://bd.1xbet.com/paysystems/deposit/?h_token=x"

    class LoggedInPage:
        def __init__(self):
            self.frames = [self, Frame()]

        url = "https://bd.1xbet.com/en/office/recharge"

        def content(self):
            return "<div>account</div>"

        def wait_for_timeout(self, ms):
            pass

    fetcher = BrowserFetcher(frame="/paysystems/deposit", logged_out_marker="registration-layout")
    target, error = fetcher._target(LoggedInPage())
    assert error is None
    assert target.url.endswith("h_token=x")


# ------------------------------------------------------- dropdown options that moved


class SelectPage(FakePage):
    """A page whose <select> offers a fixed set of option labels."""

    def __init__(self, labels):
        super().__init__()
        self.labels = labels
        self.selected = []

    def eval_on_selector(self, selector, script):
        return self.labels

    def select_option(self, selector, label=None, force=False, timeout=None):
        self.selected.append(label)


def test_a_dropdown_option_that_no_longer_exists_fails_fast_and_names_the_options():
    """A site quietly changing its bank list should cost a config line, not a 90s timeout."""
    fetcher = BrowserFetcher(
        flow=[Step(select='select[name="bank"]', option="AB Bank", wait_for=".x")]
    )
    page = SelectPage(["Select recipient's bank", "IFIC", "Islami Bank Bangladesh Limited"])
    error = fetcher._walk(page)

    assert error is not None
    assert "AB Bank" in error
    # The message has to carry what the dropdown *does* offer, or the fix is another visit.
    assert "IFIC" in error
    assert page.selected == []  # nothing was chosen


def test_a_present_option_is_selected():
    fetcher = BrowserFetcher(flow=[Step(select="#bank", option="IFIC", wait_for=".x")])
    page = SelectPage(["IFIC", "Other Bank"])
    assert fetcher._walk(page) is None
    assert page.selected == ["IFIC"]


# ------------------------------------------- capturing after the flow leaves the frame


class NavPage(FakePage):
    """A page whose URL changes partway, as a confirm-and-redirect step does."""

    def __init__(self, start, after=None, frames=()):
        super().__init__()
        self.url = start
        self._after = after
        self.frames = list(frames)

    def wait_for_timeout(self, ms):
        if self._after:
            self.url = self._after
            self._after = None


class FakeFrame:
    def __init__(self, url, detached=False):
        self.url = url
        self._detached = detached

    def is_detached(self):
        return self._detached


def test_capture_follows_the_tab_when_the_flow_navigates_away():
    """Confirming a deposit leaves the embedded app; the payee is on the page we land on."""
    fetcher = BrowserFetcher(frame="/paysystems/deposit")
    page = NavPage(
        "https://site/office/recharge",
        after="https://psp.example/check-out/abc",
        frames=[FakeFrame("https://site/paysystems/deposit/?x")],
    )
    navigated = fetcher._await_navigation(page, "https://site/office/recharge")
    assert navigated is True
    # Even though a frame by that name is still listed, the tab moved: capture the page.
    assert fetcher._capture_target(page, navigated=navigated) is page


def test_capture_stays_in_the_frame_when_the_tab_did_not_move():
    fetcher = BrowserFetcher(frame="/paysystems/deposit")
    frame = FakeFrame("https://site/paysystems/deposit/?x")
    page = NavPage("https://site/office/recharge", frames=[frame])
    assert fetcher._await_navigation(page, page.url, budget_ms=600) is False
    assert fetcher._capture_target(page, navigated=False) is frame


def test_a_detached_frame_is_never_captured_from():
    """A frame left behind by a navigation lingers in page.frames but cannot be read."""
    fetcher = BrowserFetcher(frame="/paysystems/deposit")
    dead = FakeFrame("https://site/paysystems/deposit/?x", detached=True)
    page = NavPage("https://site/office/recharge", frames=[dead])
    assert fetcher._capture_target(page, navigated=False) is page


def test_unreadable_target_falls_back_to_the_page():
    from ght.fetchers.browser import _read_html

    class Dead:
        def content(self):
            raise RuntimeError("Frame was detached")

    class Live:
        def content(self):
            return "<html>landed here</html>"

    html, note = _read_html(Dead(), Live())
    assert "landed here" in html
    assert "frame went away" in note


# ------------------------------------------------- keeping the session rolling forward


class FakeContext:
    def __init__(self, state):
        self._state = state

    def storage_state(self):
        return self._state


class FakePageUA:
    @staticmethod
    def evaluate(_script):
        return "UA/2"


def _session_file(tmp_path, cookie="old"):
    state = tmp_path / "auth.json"
    state.write_text(
        json.dumps({"storage_state": {"cookies": [cookie]}, "user_agent": "UA/1", "channel": None}),
        encoding="utf-8",
    )
    return state


def _stored(state):
    return json.loads(state.read_text(encoding="utf-8"))


def test_a_signed_in_fetch_saves_the_refreshed_session(tmp_path):
    """Sites roll their session cookie as you browse. Discarding what came back means the
    stored session only ever ages, and expires however often we collect."""
    state = _session_file(tmp_path)
    fetcher = BrowserFetcher(auth_state=str(state), logged_out_marker="registration-widget")
    fetcher._auth_kwargs()
    fetcher._refresh_session(FakeContext({"cookies": ["fresh"]}), FakePageUA(), "<html>ok</html>", None)
    assert _stored(state)["storage_state"] == {"cookies": ["fresh"]}
    assert _stored(state)["user_agent"] == "UA/2"


def test_a_logged_out_capture_never_overwrites_a_good_session(tmp_path):
    """The guard that matters: writing a logged-out state back would turn one expired
    session into a permanently broken one."""
    state = _session_file(tmp_path)
    fetcher = BrowserFetcher(auth_state=str(state), logged_out_marker="registration-widget")

    fetcher._refresh_session(FakeContext({"cookies": ["dead"]}), FakePageUA(), "<html>ok</html>", "LOGGED_OUT")
    assert _stored(state)["storage_state"] == {"cookies": ["old"]}

    fetcher._refresh_session(
        FakeContext({"cookies": ["dead"]}), FakePageUA(), "<div class=registration-widget>", None
    )
    assert _stored(state)["storage_state"] == {"cookies": ["old"]}


def test_no_session_file_means_nothing_is_written(tmp_path):
    """An anonymous run must not invent a session file out of a logged-out browser."""
    missing = tmp_path / "nope.json"
    BrowserFetcher(auth_state=str(missing))._refresh_session(
        FakeContext({"cookies": []}), FakePageUA(), "<html>ok</html>", None
    )
    assert not missing.exists()
