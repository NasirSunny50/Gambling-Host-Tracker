"""Per-site extraction config, loaded from sources/<slug>.yaml.

These files deliberately live in git rather than in the database: when a site redesigns
and someone rewrites a selector, the git history is the audit trail of what the collector
was looking for on any given date.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ght.config import settings
from ght.normalize.channel import ALL_CHANNELS


class _Strict(BaseModel):
    """Base for config models: an unknown key is a typo, and a silently ignored typo is a
    selector that never runs. Better to fail the file loudly - scan_sources isolates it so
    one bad file does not stop the others."""

    model_config = ConfigDict(extra="forbid")


class SourceUrl(_Strict):
    url: str
    type: str = "deposit"
    current: bool = True


class Block(_Strict):
    """One payment-method block on the page.

    Pairing the channel with the selector in config is what makes a hit high confidence:
    we know it is a bKash number because the config says this container is the bKash box,
    not because we guessed from nearby words.
    """

    channel: str
    value: str
    container: str | None = None
    account_type: str | None = None
    # Selector, relative to the container, for the payee/merchant name printed beside the
    # number. Optional: most bank blocks label the name well enough for the normalizer.
    holder: str | None = None
    # Canonical bank name for this block, when the page never writes it beside the number.
    # A bank-transfer panel names the recipient bank only in a dropdown listing every
    # option, so reading it off the page would be a coin flip between four banks; the
    # config selected the bank, so the config is what knows it.
    bank_name: str | None = None

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, value: str) -> str:
        if value not in ALL_CHANNELS:
            raise ValueError(f"unknown channel {value!r}; expected one of {sorted(ALL_CHANNELS)}")
        return value

    @model_validator(mode="after")
    def _holder_needs_a_container(self) -> Block:
        """A payee name is only meaningful inside the block it belongs to.

        Unscoped, the selector searches the whole document and pins the first name it finds
        onto every account on the page — including accounts it has nothing to do with. A
        wrong holder name is worse than a missing one: it is the field an investigator
        reads as the identity behind the wallet.
        """
        if self.holder and not self.container:
            raise ValueError(
                f"holder {self.holder!r} needs a container; "
                "an unscoped holder selector attributes the wrong name"
            )
        return self


class Step(_Strict):
    """One click in a multi-step deposit flow.

    Some sites do not print any account on the deposit page itself: you pick a method,
    confirm, and the number only appears on the payment provider's page you land on. The
    flow is config rather than code because each site's click path differs, and a redesign
    that moves a button should be a YAML diff in the audit trail like any other selector.
    """

    # Exactly one of these. Some payee details only appear once a dropdown is set - a
    # bank transfer panel shows nothing at all until a recipient bank is chosen - and some
    # only once an amount has been typed, because the button that reveals them stays
    # disabled until the form validates.
    click: str | None = None
    select: str | None = None
    fill: str | None = None
    # The option label to choose, when this step is a select; the text to type, when it is
    # a fill. An amount belongs in config rather than in code: it is a property of what the
    # site will accept, and the smallest one it accepts is the one to send.
    option: str | None = None
    value: str | None = None
    # Substring of the URL of the frame this step acts in, when that is not the document
    # the probe started in. A payment aggregator hands the deposit form to an iframe of its
    # own - sometimes nested inside another - and a selector aimed at the document the
    # method cell lived in matches nothing there, which looks exactly like a stale selector.
    frame: str | None = None
    # Selector to wait for afterwards, so the next step acts on a rendered page.
    wait_for: str | None = None
    # A step that may legitimately be absent (an interstitial that only shows sometimes).
    optional: bool = False

    @model_validator(mode="after")
    def _one_action(self) -> Step:
        actions = [bool(self.click), bool(self.select), bool(self.fill)]
        if sum(actions) != 1:
            raise ValueError("a step needs exactly one of click, select or fill")
        if self.select and not self.option:
            raise ValueError(f"select {self.select!r} needs an option to choose")
        if self.fill and self.value is None:
            raise ValueError(f"fill {self.fill!r} needs a value to type")
        return self

    @property
    def target(self) -> str:
        """The selector this step acts on, whichever kind of step it is."""
        return self.click or self.select or self.fill or ""


class Probe(_Strict):
    """One method's details, reached by its own clicks and read by its own selectors.

    A deposit page that shows one payee at a time needs several visits to enumerate, and
    each visit lands on different markup. Keeping the blocks beside the flow that reveals
    them is what preserves the channel guarantee: the bKash number is known to be bKash
    because it came from the block that opened the bKash panel, not from a guess about a
    page holding six panels at once.
    """

    name: str
    flow: list[Step] = Field(default_factory=list)
    wait_for: str | None = None
    blocks: list[Block] = Field(default_factory=list)
    # Selector for a payee that has a name but no account number. Recorded as a merchant
    # sighting instead of an account, because there is no number to key an identity on.
    merchant: str | None = None
    # Channel a merchant sighting belongs to. Blocks carry their own channel; a name-only
    # probe has no block to carry it.
    channel: str | None = None
    # True when reaching this probe's payee requires confirming a deposit, which initiates
    # a deposit request on the operator (no funds move, nothing is paid). Surfaced in the
    # portal so it is clear which probes have that side effect.
    creates_order: bool = False


class Login(_Strict):
    """How to sign in to a site, so an expired session can be refreshed from a run.

    Selectors only — no credentials are stored anywhere. In headless mode the flow fills the
    fields and submits, aborting if a ``challenge`` selector (CAPTCHA / 2FA) appears. In
    ``assisted`` mode a visible browser opens and the operator signs in by hand, which is the
    only thing that gets past bot protection.
    """

    url: str
    username: str
    password: str
    submit: str
    success: str
    open: str | None = None
    challenge: list[str] = Field(default_factory=list)
    # Open a visible browser and let the operator finish signing in by hand (solving the
    # CAPTCHA / 2FA themselves), rather than attempting a fully headless login. Required for
    # sites with bot protection. Needs a person at the machine with a desktop.
    assisted: bool = False


class Reset(_Strict):
    """Getting back to the method list after a probe, without reloading the panel."""

    # What closes the open modal.
    click: str
    # A selector that matches only while a modal is open. The reset is only trusted once
    # this stops matching: a half-closed modal would swallow the next probe's click and be
    # reported as that probe's selector being broken.
    gone: str


class Discovery(_Strict):
    """A family of methods found and walked at run time rather than named one by one.

    A `probe` pins a fixed method by its selector; a discovery instead enumerates whatever
    the panel is showing right now and walks each match. Two reasons it exists:

    - **New modules with familiar names.** These operators spin up payment modules under
      fresh ids constantly — "The Local Nagad", "Upay Free", tomorrow's "Super Bkash" — all
      still bKash / Nagad / Upay underneath. A fixed probe list only ever sees the ones
      written down; `kind: cells` finds every cell whose visible name carries one of the
      channel words and reads its number, so a rename or an addition is collected without a
      config edit.
    - **A dropdown whose options drift.** Bank Transfer lists its recipient banks in a
      `<select>` that changes without notice. `kind: options` walks every option the
      dropdown offers, so the bank list is read off the page instead of hard-coded and
      re-checked by hand.

    Only the modal is opened, never a deposit confirmed, so a discovery never initiates an
    order — the one paykassma method whose payee needs a confirm stays an explicit probe.
    """

    name: str
    # ``cells`` clicks each matching method cell; ``options`` selects each dropdown option.
    kind: str
    # Steps to reach the enumerable list. Empty for ``cells`` (the grid is already there);
    # for ``options`` this opens the method whose panel carries the dropdown.
    open: list[Step] = Field(default_factory=list)
    # ``cells``: selector enumerating candidate method cells, scoped to the section wanted.
    # ``options``: selector for the ``<select>`` whose options are walked.
    items: str
    # ``cells`` only: the visible-name element inside a cell, and the attribute that
    # identifies it so the same cell can be reopened after a reset.
    label: str | None = None
    key_attr: str = "data-method"
    # ``cells`` only: a cell qualifies when its name contains one of these (case-insensitive).
    # Each entry doubles as the channel the match is attributed to, so they must be channels.
    match: list[str] = Field(default_factory=list)
    # A fixed channel for every match. Required for ``options`` (which has no name to read);
    # for ``cells`` it overrides the matched-word inference.
    channel: str | None = None
    # A selector that proves the method list has finished rendering, waited for before any
    # enumeration. The embedded panel's frame appears seconds before its cells do, so a
    # discovery that queried the moment the frame existed found an empty list. Set it to
    # something every loaded panel shows (any method cell); a section that then holds no
    # matching cell is genuinely empty and returns nothing at once, without a timeout.
    ready: str | None = None
    # Modal-ready selector to wait for after opening a match, raced against the site's
    # own "unavailable" panel exactly as a probe's ``wait_for`` is.
    wait_for: str | None = None
    # The number/value element inside the opened modal, the box it sits in (so the name and
    # account-type reader sees only this method's text, not the whole panel), and an
    # optional holder beside it.
    value: str
    container: str | None = None
    holder: str | None = None
    account_type: str | None = None
    # ``options`` only: option labels or values to pass over — the "choose a bank" placeholder.
    skip_options: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in {"cells", "options"}:
            raise ValueError(f"discovery kind must be 'cells' or 'options', not {value!r}")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> Discovery:
        if self.kind == "cells":
            if not self.label:
                raise ValueError("a cells discovery needs a label selector to read each name")
            for word in self.match:
                if word not in ALL_CHANNELS:
                    raise ValueError(
                        f"discovery match {word!r} is used as a channel; "
                        f"expected one of {sorted(ALL_CHANNELS)}"
                    )
            if not self.match and not self.channel:
                raise ValueError("a cells discovery needs match words or a fixed channel")
        else:  # options
            if not self.channel:
                raise ValueError("an options discovery needs a channel")
        if self.channel and self.channel not in ALL_CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        return self


class SourceConfig(_Strict):
    slug: str
    name: str
    status: str = "active"
    fetcher: str = "http"  # http | browser
    urls: list[SourceUrl] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    # Clicks to walk before capturing. Browser fetcher only; ignored by the HTTP fetcher.
    flow: list[Step] = Field(default_factory=list)
    # Selector that marks the final page as ready, checked after the flow finishes.
    wait_for: str | None = None
    # Path to a Playwright storage_state JSON for sites whose deposit page needs a login.
    # Written by the sign-in step at the start of a run; no credentials live in config.
    auth_state: str | None = None
    # How to sign in to refresh an expired session. Without it, a run that finds the
    # session dead can only report it.
    login: Login | None = None
    # Force a specific browser for the browser fetcher (msedge / chrome). Left unset, the
    # fetcher tries the bundled Chromium first and falls back to installed browsers.
    browser_channel: str | None = None
    # Substring of the URL of the iframe holding the deposit UI. Payment panels are often
    # a separate app embedded in the account page, and neither the flow clicks nor the
    # selectors reach into it from the top-level document.
    frame: str | None = None
    # Per-page timeout in seconds for the browser fetcher. A heavy account page that loads
    # its payment panel in an iframe can take well over the default; keeping this in config
    # means the site collects the same from the CLI, the script, or the portal, instead of
    # depending on an environment variable someone has to remember to set.
    timeout: int | None = None
    # Per-method probes. A source uses either these or the single flow/blocks pair above.
    probes: list[Probe] = Field(default_factory=list)
    # Families of methods enumerated and walked at run time — new modules under familiar
    # names, and dropdown options that drift. Run inside the same loaded panel the probes
    # use, after them.
    discover: list[Discovery] = Field(default_factory=list)
    # How to put the panel back the way it was between probes. Every probe needs the
    # method list, and reaching it again by reloading the page costs the iframe's whole
    # start-up - measured at ten to thirteen seconds, which was most of a run. Closing the
    # open modal instead keeps one loaded panel for every method.
    reset: Reset | None = None
    # Selectors that mean the site itself has switched a method off - it renders a
    # "not available" panel instead of the payee. That is the operator's decision, not a
    # stale selector on our side, and the difference decides whether a run is reported as
    # healthy or as needing a config fix. Checked the moment a flow step lands, so a
    # disabled method costs seconds instead of the full page timeout.
    unavailable: list[str] = Field(default_factory=list)
    # Per-step timeout in seconds for flow clicks. Separate from ``timeout`` on purpose:
    # the embedded panel legitimately takes a minute to appear, but a button that is going
    # to be there is there in seconds, so a missing one should not cost the page budget.
    flow_timeout: int | None = None
    # Substring present only on the logged-out page. Its appearance means the saved session
    # has expired, so the run stops fast with a clear message instead of timing out on
    # every probe waiting for a deposit panel that will never load.
    logged_out_marker: str | None = None
    # Substring of the landed URL that means the session expired — a site that answers a
    # request for the account page by redirecting to its login page. More reliable than a
    # body marker, since the login page shares no markup with the deposit page.
    logged_out_url: str | None = None
    # Substrings the page *around* the panel shows when the payment app itself refuses the
    # session — the site still serves the signed-in shell and still embeds the panel, and
    # only the embedded app says no. Neither marker above sees that: the shell is not the
    # logged-out layout and the URL never changes. Without it a refused session looks like
    # every selector in the config breaking at once, and the run walks each method into the
    # same blocking overlay at full timeout before reporting a config fix that would not
    # help. Matched against the top document, because the overlay is drawn outside the
    # frame the probes work in.
    session_expired: list[str] = Field(default_factory=list)
    # The element to photograph, when a page is mostly not the payee. These deposit pages
    # run to thousands of pixels of lobby around one small panel, and a reviewer opening the
    # evidence needs the panel. Unset, or absent from the page, means capture all of it -
    # which is right for a provider's checkout, where the payee *is* the page.
    shot: str | None = None
    ignore_numbers: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("fetcher")
    @classmethod
    def _known_fetcher(cls, value: str) -> str:
        if value not in {"http", "browser"}:
            raise ValueError(f"unknown fetcher {value!r}")
        return value

    @model_validator(mode="after")
    def _flow_needs_browser(self) -> SourceConfig:
        """A click flow is meaningless without a browser to click with.

        Catching this at load time matters: an http-fetcher site with a flow would run
        happily, extract nothing, and look like a site with no accounts rather than a
        misconfiguration.
        """
        if (self.flow or self.probes) and self.fetcher != "browser":
            raise ValueError(f"flow requires fetcher: browser, got {self.fetcher!r}")
        if self.probes and (self.flow or self.blocks):
            raise ValueError(
                "use either probes or a single flow/blocks pair, not both: "
                "with probes configured the top-level flow and blocks would never run"
            )
        if self.discover:
            if self.fetcher != "browser":
                raise ValueError(f"discover requires fetcher: browser, got {self.fetcher!r}")
            if self.reset is None:
                raise ValueError(
                    "discover walks several methods through one loaded panel and so needs a "
                    "reset, the same as probes do"
                )
        return self

    @property
    def current_urls(self) -> list[str]:
        """Current URLs first, then the known mirrors as fallbacks."""
        return [u.url for u in self.urls if u.current] + [u.url for u in self.urls if not u.current]


def load_source(slug: str, sources_dir: Path | None = None) -> SourceConfig:
    path = (sources_dir or settings.sources_dir) / f"{slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no source config at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SourceConfig.model_validate(data)


@dataclass(frozen=True)
class BrokenSource:
    """A config file that would not parse, kept so the CLI can report it."""

    path: Path
    error: str


def scan_sources(sources_dir: Path | None = None) -> tuple[list[SourceConfig], list[BrokenSource]]:
    """Load every site config, separating the good files from the broken ones.

    A typo in one YAML must not stop collection from every other site: these operators
    rotate accounts daily, and a day of missed collection cannot be gone back for. So the
    broken file is reported and the rest of the run proceeds.

    Files whose names start with an underscore are skipped entirely.
    """
    directory = sources_dir or settings.sources_dir
    configs: list[SourceConfig] = []
    broken: list[BrokenSource] = []

    for path in sorted(directory.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            configs.append(SourceConfig.model_validate(data))
        except Exception as exc:  # noqa: BLE001 - a bad file is reported, not fatal
            broken.append(BrokenSource(path=path, error=_first_error_line(exc)))
    return configs, broken


def _first_error_line(exc: Exception) -> str:
    """Pydantic renders a multi-line report; the CLI table only has room for the reason."""
    for line in str(exc).splitlines():
        stripped = line.strip()
        if stripped.startswith("Value error, "):
            return stripped.removeprefix("Value error, ").split(" [type=")[0]
    return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"


def load_all_sources(sources_dir: Path | None = None) -> list[SourceConfig]:
    """Every config that parses. Use :func:`scan_sources` when the failures matter too."""
    configs, _ = scan_sources(sources_dir)
    return configs
