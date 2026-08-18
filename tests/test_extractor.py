"""Extraction against a saved page — offline, deterministic, no network.

Every real target gets a fixture here after recon. When a site redesigns, the new page is
saved alongside the old one so the regression stays visible.
"""

from pathlib import Path

import pytest

from ght.extractors.base import extract
from ght.extractors.regex_sweep import sweep
from ght.sources import Block, SourceConfig, load_source
from ght.types import CONFIDENCE_HIGH

FIXTURES = Path(__file__).parent / "fixtures" / "html"
SOURCES = Path(__file__).parents[1] / "sources"


@pytest.fixture(scope="module")
def config():
    return load_source("demo-site", SOURCES)


@pytest.fixture(scope="module")
def html():
    return (FIXTURES / "demo_site_deposit.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def result(html, config):
    return extract(html, config)


def test_every_configured_block_yields_one_account(result):
    assert result.selector_hits == 4
    assert len(result.accounts) == 4


def test_channels_and_numbers(result):
    by_channel = {account.channel: account.account_number for account in result.accounts}
    assert by_channel == {
        "bkash": "+8801712345678",
        "nagad": "+8801812345678",  # written in Bangla numerals on the page
        "rocket": "+8801912345678",  # written as "+880 1912-345678"
        "bank_transfer": "20501234567890123",  # written spaced across groups
    }


def test_selector_hits_are_high_confidence_and_need_no_review(result):
    assert all(account.confidence >= CONFIDENCE_HIGH for account in result.accounts)
    assert result.review_count == 0


def test_account_type_and_operator_come_from_context(result):
    by_channel = {account.channel: account for account in result.accounts}
    assert by_channel["bkash"].account_type == "personal"
    assert by_channel["nagad"].account_type == "agent"
    assert by_channel["bkash"].operator == "Grameenphone"
    assert by_channel["rocket"].operator == "Banglalink"


def test_bank_details_are_parsed(result):
    bank = next(a for a in result.accounts if a.channel == "bank_transfer")
    assert bank.bank_name == "Islami Bank Bangladesh"
    assert bank.branch == "Motijheel"
    assert bank.holder_name == "Rahim Enterprise"


def test_support_hotline_is_excluded_by_config(result):
    assert "+8801500000000" not in {a.account_number for a in result.accounts}


def test_ticket_id_is_not_taken_as_a_bank_account(result):
    assert "998877665544" not in {a.account_number for a in result.accounts}


def test_script_contents_are_not_scanned(html):
    # The page has a tracking number inside a <script> tag.
    assert "01999999999" in html
    assert "+8801999999999" not in {c.raw_text for c in sweep(html)}


def test_sweep_does_not_duplicate_numbers_the_selectors_claimed(result):
    numbers = [account.account_number for account in result.accounts]
    assert len(numbers) == len(set(numbers))
    assert result.sweep_hits == 0


def test_stale_selectors_are_detected(html, config):
    """A site redesign must not look like a quiet day with no numbers."""
    stale = config.model_copy(
        update={"blocks": [b.model_copy(update={"container": ".gone"}) for b in config.blocks]}
    )
    result = extract(html, stale)

    assert result.selector_hits == 0
    assert result.sweep_hits > 0
    assert result.extractor_looks_broken is True


def test_sweep_alone_still_recovers_every_number(html, config):
    """The fallback keeps the data flowing while a broken selector is being fixed."""
    no_blocks = config.model_copy(update={"blocks": []})
    result = extract(html, no_blocks)

    numbers = {account.account_number for account in result.accounts}
    assert numbers == {
        "+8801712345678",
        "+8801812345678",
        "+8801912345678",
        "20501234567890123",
    }
    # Recovered, but nothing vouches for the channel, so none of it is auto-trusted.
    assert result.review_count == len(result.accounts)


def test_healthy_page_is_not_reported_as_broken(result):
    assert result.extractor_looks_broken is False


def test_a_configured_bank_does_not_turn_amount_presets_into_an_account():
    """Knowing the bank skips the bank lookup, not the checks that reject non-accounts."""
    html = """
      <div class="panel">
        <div class="val">Please enter or select your deposit amount 1 000 2 000 5 000 7 000 10 000</div>
      </div>
    """
    config = SourceConfig(
        slug="t",
        name="t",
        fetcher="browser",
        blocks=[
            Block(channel="bank_transfer", container=".panel", value=".val", bank_name="AB Bank")
        ],
    )
    assert extract(html, config).accounts == []


def test_a_holder_selector_landing_on_the_number_is_refused():
    """One element class carries every printed value, so the selector can hit the number."""
    html = """
      <div class="row">
        <b>wallet number</b><span class="val">01712345678</span>
      </div>
    """
    config = SourceConfig(
        slug="t",
        name="t",
        fetcher="browser",
        blocks=[Block(channel="bkash", container=".row", value=".row", holder=".val")],
    )
    account = extract(html, config).accounts[0]
    assert account.account_number == "+8801712345678"
    assert account.holder_name is None
