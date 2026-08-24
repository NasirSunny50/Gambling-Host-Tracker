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

    def wait_for_timeout(self, ms):
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


# --------------------------------------------- a method the site has switched off


class _Visible:
    """A minimal element handle: present in the DOM, and possibly on screen."""

    def __init__(self, visible: bool):
        self._visible = visible

    def is_visible(self):
        return self._visible


class FakePageWithMarkers(FakePage):
    """A page that answers a click with whichever panel the site decided to render.

    ``present`` is what is in the DOM; ``visible`` is what the user can see. They differ
    after a modal closes - its markup stays behind - which is exactly the case that made
    a shared panel report every later method as switched off.
    """

    def __init__(self, present=(), failing=None, visible=None):
        super().__init__(failing=failing)
        self.present = set(present)
        self.visible = set(self.present if visible is None else visible)

    def wait_for_selector(self, selector, timeout=None):
        self.waited.append(selector)
        # Playwright takes a comma-joined selector as "any of these"; so does this.
        wanted = [part.strip() for part in selector.split(",")]
        if any(part in self.present for part in wanted):
            return
        raise TimeoutError(f"no element matching {selector}")

    def query_selector(self, selector):
        return _Visible(selector in self.visible) if selector in self.present else None


def test_a_switched_off_method_is_not_reported_as_a_broken_flow():
    """The site renders its own "unavailable" panel. Nothing here is broken, so the walk
    ends cleanly and says which panel it got instead of blaming a selector."""
    fetcher = BrowserFetcher(
        flow=[Step(click='.payment-cell[data-method="upay_bangla"]', wait_for=".payee")],
        unavailable=[".modal-payment--method-undefined"],
    )
    page = FakePageWithMarkers(
        present={'.payment-cell[data-method="upay_bangla"]', ".modal-payment--method-undefined"}
    )
    assert fetcher._walk(page) is None
    assert fetcher._unavailable_hit == ".modal-payment--method-undefined"


def test_the_expected_panel_still_wins_when_it_is_the_one_that_rendered():
    fetcher = BrowserFetcher(
        flow=[Step(click=".payment-cell", wait_for=".payee")],
        unavailable=[".modal-payment--method-undefined"],
    )
    page = FakePageWithMarkers(present={".payment-cell", ".payee"})
    assert fetcher._walk(page) is None
    assert fetcher._unavailable_hit is None


def test_a_leftover_marker_from_the_last_method_is_not_this_method_being_off():
    """The closed modal keeps its classes. Reading presence rather than visibility made a
    shared panel report every method after the first closed one as switched off, and
    collect nothing at all."""
    fetcher = BrowserFetcher(
        flow=[Step(click=".payment-cell", wait_for=".payee")],
        unavailable=[".modal-payment--method-undefined"],
    )
    page = FakePageWithMarkers(
        present={".payment-cell", ".payee", ".modal-payment--method-undefined"},
        visible={".payment-cell", ".payee"},  # the old modal is closed but still in the DOM
    )
    assert fetcher._walk(page) is None
    assert fetcher._unavailable_hit is None


def test_the_expected_panel_beats_a_marker_that_is_also_showing():
    """Mid-swap the site's one modal element is visible wearing the last method's class
    while already showing this method's payee. The payee is the answer."""
    fetcher = BrowserFetcher(
        flow=[Step(click=".payment-cell", wait_for=".payee")],
        unavailable=[".modal-payment--method-undefined"],
    )
    page = FakePageWithMarkers(
        present={".payment-cell", ".payee", ".modal-payment--method-undefined"},
        visible={".payment-cell", ".payee", ".modal-payment--method-undefined"},
    )
    assert fetcher._walk(page) is None
    assert fetcher._unavailable_hit is None


def test_a_missing_button_is_still_a_broken_flow():
    """The site declaring a method off is not the same as the button having vanished -
    the second one is ours to fix, and must keep saying so."""
    fetcher = BrowserFetcher(
        flow=[Step(click=".gone", wait_for=".payee")],
        unavailable=[".modal-payment--method-undefined"],
    )
    page = FakePageWithMarkers(present=set())
    error = fetcher._walk(page)
    assert error is not None and ".gone" in error
    assert fetcher._unavailable_hit is None


def test_clicks_use_the_shorter_budget_not_the_page_one():
    """Eight probes waiting a 90s page budget for a button was most of an eight-minute run."""
    fetcher = BrowserFetcher(timeout=90, flow_timeout=25, flow=[Step(click=".x")])
    assert fetcher.timeout == 90_000
    assert fetcher.flow_timeout == 25_000


def test_without_its_own_setting_a_click_may_take_the_page_budget():
    fetcher = BrowserFetcher(timeout=90, flow=[Step(click=".x")])
    assert fetcher.flow_timeout == 90_000


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


class FillPage(FakePage):
    """A form that only enables its button once something has been typed into it."""

    def __init__(self):
        super().__init__()
        self.typed: list[tuple[str, str]] = []
        self.cleared: list[str] = []

    def fill(self, selector, value, timeout=None):
        self.cleared.append(selector)

    def type(self, selector, text, delay=None, timeout=None):
        self.typed.append((selector, text))


def test_an_amount_is_typed_rather_than_assigned():
    """These forms enable the next button from the input's own key events. A value set
    straight onto the element leaves the button disabled, and the step that follows is then
    reported as a selector that no longer matches - which sends someone to fix the wrong
    thing entirely."""
    fetcher = BrowserFetcher(flow=[Step(fill="#amount", value="200", wait_for=".next")])
    page = FillPage()

    assert fetcher._walk(page) is None
    assert page.typed == [("#amount", "200")]
    # Cleared first: a prefilled default would otherwise leave "500200" in the box.
    assert page.cleared == ["#amount"]


def test_a_step_does_exactly_one_thing():
    for kwargs in (
        {},  # nothing at all
        {"click": ".a", "fill": "#b", "value": "1"},
        {"click": ".a", "select": "#b", "option": "x"},
    ):
        with pytest.raises(ValidationError):
            Step(**kwargs)


def test_a_fill_without_a_value_is_a_config_error_not_an_empty_box():
    with pytest.raises(ValidationError) as caught:
        Step(fill="#amount")
    assert "value" in str(caught.value)


def test_an_amount_of_zero_is_a_value_like_any_other():
    """`value` is checked for being absent, not for being falsy - "0" is a thing a site
    might legitimately want typed, and `if not value` would reject it."""
    assert Step(fill="#amount", value="0").value == "0"


class FramePage(FakePage):
    """A tab whose frames appear only after the click that creates them."""

    def __init__(self, frames_after_click=("https://plugin.example/nagad_api",)):
        super().__init__()
        self.frames = [self]
        self._pending = list(frames_after_click)
        self.url = "https://site.example/office/recharge"

    def click(self, selector, timeout=None):
        self.clicked.append(selector)
        for url in self._pending:
            self.frames.append(PluginFrame(url, self))
        self._pending = []

    def is_detached(self):
        return False


class PluginFrame(FakePage):
    def __init__(self, url, tab):
        super().__init__()
        self.url = url
        self.tab = tab
        self.typed = []

    def is_detached(self):
        return False

    def fill(self, selector, value, timeout=None):
        pass

    def type(self, selector, text, delay=None, timeout=None):
        self.typed.append((selector, text))


def test_a_step_can_move_the_flow_into_another_frame():
    """An aggregator hands its deposit form to an iframe of its own. Without following it,
    every selector after that click matches nothing and reports a stale config."""
    fetcher = BrowserFetcher(
        flow=[
            Step(click=".method"),
            Step(frame="plugin.example", fill="input[name=amount]", value="200"),
        ]
    )
    tab = FramePage()

    assert fetcher._walk(tab, tab) is None
    inner = tab.frames[-1]
    assert inner.typed == [("input[name=amount]", "200")]
    # The capture has to come from the frame the flow ended in, not the one it started in.
    assert fetcher._capture_target(tab, navigated=False) is inner


def test_a_frame_that_never_appears_is_named_rather_than_waited_out_silently():
    fetcher = BrowserFetcher(
        flow=[Step(frame="never-shows-up.example", click=".x")], flow_timeout=1
    )
    tab = FramePage(frames_after_click=())

    error = fetcher._walk(tab, tab)
    assert error is not None
    assert "never-shows-up.example" in error


def test_a_flow_with_no_frame_step_stays_where_it_started():
    fetcher = BrowserFetcher(flow=[Step(click=".method", wait_for=".panel")])
    page = FakePage()
    assert fetcher._walk(page) is None
    assert page.clicked == [".method"]


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


# ------------------------------------ sharing one loaded panel between probes


class FakePanel:
    """A page with a modal that may or may not close when asked."""

    def __init__(self, open_modal=True, closes=True, close_button=True):
        self.open_modal = open_modal
        self.closes = closes
        self.close_button = close_button
        self.clicked = 0

    def query_selector(self, selector):
        if selector == ".modal-payment.active":
            return object() if self.open_modal else None
        if selector == ".modal-payment__close":
            return _CloseHandle(self) if self.close_button else None
        return None

    def wait_for_selector(self, selector, state=None, timeout=None):
        still_open = selector == ".modal-payment.active" and state == "detached" and self.open_modal
        if still_open:
            raise TimeoutError("modal is still open")


class _CloseHandle:
    def __init__(self, panel):
        self.panel = panel

    def click(self, timeout=None):
        self.panel.clicked += 1
        if self.panel.closes:
            self.panel.open_modal = False


def _panel_fetcher():
    from ght.sources import Reset

    return BrowserFetcher(reset=Reset(click=".modal-payment__close", gone=".modal-payment.active"))


def test_a_closed_modal_lets_the_next_probe_reuse_the_panel():
    fetcher = _panel_fetcher()
    panel = FakePanel(open_modal=True, closes=True)
    assert fetcher._reset_panel(panel) is True
    assert panel.clicked == 1


def test_a_modal_that_will_not_close_forces_a_reload():
    """The dangerous case. Clicking the next method through a half-open modal would record
    that probe as broken - or worse, answer it with the modal still showing the last one."""
    fetcher = _panel_fetcher()
    panel = FakePanel(open_modal=True, closes=False)
    assert fetcher._reset_panel(panel) is False


def test_a_missing_close_button_forces_a_reload():
    fetcher = _panel_fetcher()
    panel = FakePanel(open_modal=True, close_button=False)
    assert fetcher._reset_panel(panel) is False


def test_nothing_open_means_the_panel_is_already_reachable():
    fetcher = _panel_fetcher()
    panel = FakePanel(open_modal=False)
    assert fetcher._reset_panel(panel) is True
    assert panel.clicked == 0


def test_without_a_configured_reset_the_panel_is_never_shared():
    fetcher = BrowserFetcher()
    assert fetcher._reset_panel(FakePanel(open_modal=False)) is False


def test_a_source_without_a_reset_keeps_fetching_one_probe_at_a_time():
    """Sharing needs a proven way back to the method list. Without one the old path, which
    reloads for every probe, is slower and still correct - so it stays the default."""
    from ght.pipeline.run import _collect_in_one_visit
    from ght.sources import Probe

    config = SourceConfig(
        slug="x", name="X", fetcher="browser",
        urls=[SourceUrl(url="https://x.invalid/")],
        probes=[Probe(name="a", wait_for="#a"), Probe(name="b", wait_for="#b")],
    )
    assert _collect_in_one_visit(config, list(config.probes), None) is None

# ------------------------------------------------------------------ what gets photographed


class ShotPage:
    """A page with one photographable panel on it."""

    def __init__(self, panel_visible=True, has_panel=True):
        self.panel_visible = panel_visible
        self.has_panel = has_panel
        self.full_page_shots = 0

    def query_selector(self, selector):
        if not self.has_panel:
            return None
        page = self

        class Element:
            @staticmethod
            def is_visible():
                return page.panel_visible

            @staticmethod
            def screenshot():
                return b"just-the-panel"

        return Element()

    def screenshot(self, full_page=False):
        self.full_page_shots += 1
        return b"the-whole-lobby"


def test_the_picture_is_of_the_panel_that_names_the_payee():
    """A deposit page is thousands of pixels of lobby around one small panel. The panel is
    the evidence; the lobby is what someone has to scroll past to find it."""
    fetcher = BrowserFetcher(shot=".modal-payment.active")
    page = ShotPage()

    assert fetcher._shoot(page, page) == b"just-the-panel"
    assert page.full_page_shots == 0


def test_a_page_with_no_panel_is_photographed_whole():
    """The same probe list ends on a provider's own checkout, where there is no panel and
    the page itself is the payee."""
    fetcher = BrowserFetcher(shot=".modal-payment.active")
    page = ShotPage(has_panel=False)

    assert fetcher._shoot(page, page) == b"the-whole-lobby"


def test_a_panel_that_is_present_but_not_on_screen_is_not_the_picture():
    """A closed modal keeps its markup. Photographing it would return a blank rectangle
    and file it as evidence."""
    fetcher = BrowserFetcher(shot=".modal-payment.active")
    page = ShotPage(panel_visible=False)

    assert fetcher._shoot(page, page) == b"the-whole-lobby"


def test_without_a_configured_panel_the_whole_page_is_the_picture():
    fetcher = BrowserFetcher()
    page = ShotPage()

    assert fetcher._shoot(page, page) == b"the-whole-lobby"


def test_screenshots_can_be_switched_off_entirely():
    fetcher = BrowserFetcher(shot=".modal-payment.active", screenshot=False)
    page = ShotPage()

    assert fetcher._shoot(page, page) is None
    assert page.full_page_shots == 0


# ------------------------------------------- the panel that refuses an expired session


REFUSAL = 'text="The session has expired"'


class RefusingPage:
    """A page whose payment frame loads and is then covered by the site's own dialog.

    The case the logged-out marker cannot see: the shell is the signed-in one, the URL
    never changes, the method list renders — and every click lands on a dialog drawn
    outside the frame.
    """

    url = "https://bd.1xbet.com/en/office/recharge"

    def __init__(self, refused: bool = True, visible: bool = True):
        class Frame:
            url = "https://bd.1xbet.com/paysystems/deposit/?h_token=x"

        self.frames = [self, Frame()]
        self.refused = refused
        self.dialog_visible = visible
        self.waited = 0

    def content(self):
        return "<div>account 177…</div>"

    def query_selector(self, selector):
        if selector == REFUSAL and self.refused:
            return _Handle(self.dialog_visible)
        return None

    def wait_for_timeout(self, ms):
        self.waited += ms


class _Handle:
    def __init__(self, visible: bool):
        self._visible = visible

    def is_visible(self):
        return self._visible


def test_a_refused_session_is_read_as_signed_out_not_as_a_broken_selector():
    """The whole point: the frame is there, so every other signal says we are in."""
    fetcher = BrowserFetcher(frame="/paysystems/deposit", session_expired=[REFUSAL])
    _target, error = fetcher._target(RefusingPage())
    assert error == "LOGGED_OUT"


def test_the_dialog_only_counts_while_it_is_up():
    """Its markup stays in the page after it closes. Believing presence would open a
    sign-in window on every healthy run."""
    fetcher = BrowserFetcher(frame="/paysystems/deposit", session_expired=[REFUSAL])
    target, error = fetcher._target(RefusingPage(refused=True, visible=False))
    assert error is None
    assert target.url.endswith("h_token=x")


def test_a_site_with_no_refusal_configured_is_unaffected():
    fetcher = BrowserFetcher(frame="/paysystems/deposit")
    _target, error = fetcher._target(RefusingPage())
    assert error is None


# ------------------------------------------------ waiting for a navigation nobody made


class StillPage:
    """A page that stays exactly where it was — every probe that does not confirm."""

    def __init__(self):
        self.url = "https://bd.1xbet.com/en/office/recharge"
        self.waited = 0

    def wait_for_timeout(self, ms):
        self.waited += ms


def test_a_probe_that_never_navigates_does_not_wait_out_the_navigation_budget():
    """Paid once per probe, on every probe: fourteen of these was over a minute of a run
    spent waiting for something that by design never happens."""
    fetcher = BrowserFetcher()
    fetcher._expects_navigation = False
    page = StillPage()
    assert fetcher._await_navigation(page, page.url) is False
    assert page.waited <= BrowserFetcher.NAV_PEEK_MS


def test_a_probe_that_confirms_a_deposit_still_gets_the_long_budget():
    """That one really does hand off to the provider's site, and it is not quick."""
    fetcher = BrowserFetcher()
    fetcher._expects_navigation = True
    page = StillPage()
    assert fetcher._await_navigation(page, page.url) is False
    assert page.waited >= BrowserFetcher.NAV_BUDGET_MS - 300


# =========================================================== discovery: cells and options


class _Text:
    def __init__(self, text):
        self._t = text

    def inner_text(self):
        return self._t


class _Shown:
    def is_visible(self):
        return True


class _Cell:
    def __init__(self, label, method):
        self.label = label
        self.method = method

    def query_selector(self, _sel):
        return _Text(self.label)

    def get_attribute(self, name):
        return self.method if name == "data-method" else None

    def click(self, timeout=None):
        pass


class _Option:
    def __init__(self, value, label):
        self.value = value
        self.label = label

    def get_attribute(self, name):
        return self.value if name == "value" else None

    def inner_text(self):
        return self.label


class _Select:
    def __init__(self, options):
        self._options = options

    def query_selector_all(self, _sel):
        return self._options

    def is_visible(self):
        return True


GONE = ".modal-payment--is-open"


class DiscoveryPanel:
    """A stand-in panel that answers the queries a discovery makes, no browser involved."""

    url = "https://bd.1xbet.com/paysystems/deposit/"

    def __init__(self, cells=None, options=None):
        self._cells = cells or []
        self._options = options or []
        self.opened = []
        self.selected = []

    def query_selector_all(self, selector):
        if "option" in selector:
            return self._options
        return list(self._cells)

    def query_selector(self, selector):
        if selector == GONE:  # the reset's "is a modal open?" check — nothing is
            return None
        if "select" in selector:
            return _Select(self._options)
        if 'data-method="' in selector:
            method = selector.split('data-method="', 1)[1].split('"', 1)[0]
            for cell in self._cells:
                if cell.method == method:
                    self.opened.append(method)
                    return cell
            return None
        return _Shown()

    def select_option(self, selector, value=None, force=False, timeout=None):
        self.selected.append(value)

    def wait_for_selector(self, selector, timeout=None, state=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def click(self, selector, timeout=None):
        self.opened.append(selector)

    def content(self):
        return "<html><body>modal</body></html>"


def _discovery_fetcher():
    from ght.sources import Reset

    return BrowserFetcher(
        screenshot=False,
        frame=None,
        reset=Reset(click=".modal-payment__close", gone=GONE),
    )


def _cells_discovery(**over):
    from ght.sources import Discovery

    base = {
        "name": "e-wallet",
        "kind": "cells",
        "items": ".section .payment-cell",
        "label": ".title",
        "match": ["bkash", "nagad", "upay"],
        "wait_for": ".modal-payment.active .value",
        "container": ".modal-payment.active",
        "value": ".modal-payment.active .value",
    }
    base.update(over)
    return Discovery(**base)


def test_a_cells_discovery_walks_only_the_names_that_carry_a_channel_word():
    """The whole point: a module named "The Local Nagad" or "Upay Free" is walked without
    ever being written down, while AIRTM beside it is left alone."""
    panel = DiscoveryPanel(
        cells=[
            _Cell("Bkash", "bt_bangladesh_default"),
            _Cell("AIRTM", "airtm"),
            _Cell("The Local Nagad", "wallet_nagad_bdt_1_new"),
            _Cell("Upay Free", "upay_free_bdt"),
        ]
    )
    found = _discovery_fetcher()._discover_cells(panel, ["https://x/"], _cells_discovery())

    assert [f.name for f in found] == [
        "e-wallet: Bkash",
        "e-wallet: The Local Nagad",
        "e-wallet: Upay Free",
    ]
    assert [f.channel for f in found] == ["bkash", "nagad", "upay"]
    # AIRTM's cell was never opened.
    assert "airtm" not in panel.opened
    # Each match carries the selectors the pipeline reads it by.
    assert found[0].value == ".modal-payment.active .value"


def test_a_cells_discovery_can_pin_one_channel_instead_of_reading_it():
    panel = DiscoveryPanel(cells=[_Cell("Some Bank Wallet", "bankx")])
    disc = _cells_discovery(match=[], channel="bank_transfer")
    found = _discovery_fetcher()._discover_cells(panel, ["https://x/"], disc)
    assert [f.channel for f in found] == ["bank_transfer"]


def test_an_options_discovery_walks_every_bank_and_names_it():
    from ght.sources import Discovery, Step

    panel = DiscoveryPanel(
        options=[
            _Option("", "Select recipient's bank"),
            _Option("dbbl", "Dutch-Bangla Bank Limited"),
            _Option("ific", "IFIC Bank"),
        ]
    )
    disc = Discovery(
        name="bank",
        kind="options",
        channel="bank_transfer",
        open=[Step(click=".payment-cell", wait_for=".modal-payment.active select")],
        items=".modal-payment.active select",
        skip_options=["", "Select recipient's bank"],
        wait_for=".modal-payment.active .value",
        container=".modal-payment.active",
        value=".modal-payment.active .value",
    )
    found = _discovery_fetcher()._discover_options(panel, disc)

    assert [f.bank_name for f in found] == ["Dutch-Bangla Bank Limited", "IFIC Bank"]
    assert all(f.channel == "bank_transfer" for f in found)
    # The placeholder was skipped, the two real banks were selected.
    assert panel.selected == ["dbbl", "ific"]


def test_infer_channel_prefers_a_fixed_channel_over_the_name():
    disc = _cells_discovery(match=["bkash"], channel="nagad")
    assert _discovery_fetcher()._infer_channel("Some Bkash Thing", disc) == "nagad"


def test_a_discovered_capture_extracts_like_a_probe(tmp_path):
    """End to end on real markup: a discovered bKash modal yields the bKash number, proving
    the run-time-built block reads the page the same way a hand-written probe would."""
    from ght.fetchers.browser import Discovered
    from ght.pipeline.run import _discovered_capture
    from ght.types import RawCapture

    modal = (
        "<html><body><div class='modal-payment active'>"
        "<div class='payment_modal_input--html'>01969660877</div>"
        "<div class='modal-message-address'>SHOP NAME</div>"
        "</div></body></html>"
    )
    config = SourceConfig(
        slug="1xbet-bd",
        name="x",
        fetcher="browser",
        urls=[SourceUrl(url="https://x.invalid/")],
    )
    found = Discovered(
        name="e-wallet: Bkash",
        channel="bkash",
        capture=RawCapture(url="https://x/", status_code=200, html=modal),
        value=".modal-payment.active .payment_modal_input--html, .modal-payment.active .modal-message-address",
        container=".modal-payment.active",
    )
    probe, probe_config, capture = _discovered_capture(config, found)
    assert probe.name == "e-wallet: Bkash"
    account = extract(capture.html, probe_config).accounts[0]
    assert account.channel == "bkash"
    assert account.account_number == "+8801969660877"


# ---------------------------------------------------- discovery config is checked at load


def test_a_cells_discovery_without_a_label_is_rejected():
    from ght.sources import Discovery

    with pytest.raises(ValidationError):
        Discovery(name="x", kind="cells", items=".cell", match=["bkash"], value=".v")


def test_a_cells_discovery_match_must_be_a_channel():
    from ght.sources import Discovery

    with pytest.raises(ValidationError):
        Discovery(name="x", kind="cells", items=".cell", label=".t", match=["paytm"], value=".v")


def test_an_options_discovery_needs_a_channel():
    from ght.sources import Discovery

    with pytest.raises(ValidationError):
        Discovery(name="x", kind="options", items="select", value=".v")


def test_an_unknown_discovery_kind_is_rejected():
    from ght.sources import Discovery

    with pytest.raises(ValidationError):
        Discovery(name="x", kind="both", items=".cell", label=".t", match=["bkash"], value=".v")


def test_discover_requires_a_reset_to_share_the_panel():
    from ght.sources import Discovery

    with pytest.raises(ValidationError):
        SourceConfig(
            slug="s",
            name="s",
            fetcher="browser",
            urls=[SourceUrl(url="https://x.invalid/")],
            discover=[
                Discovery(
                    name="e", kind="cells", items=".cell", label=".t",
                    match=["bkash"], value=".v",
                )
            ],
        )
