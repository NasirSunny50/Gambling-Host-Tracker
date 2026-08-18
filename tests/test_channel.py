"""Channel classification — the channel always comes from page context, never the number."""

import pytest

from ght.normalize.channel import (
    CHANNEL_BANK,
    CHANNEL_BKASH,
    CHANNEL_NAGAD,
    CHANNEL_ROCKET,
    CHANNEL_UPAY,
    channel_near,
    classify_account_type,
    classify_channel,
)


@pytest.mark.parametrize(
    "context,expected",
    [
        ("bKash Personal", CHANNEL_BKASH),
        ("b-kash deposit", CHANNEL_BKASH),
        ("বিকাশ নম্বর", CHANNEL_BKASH),
        ("Nagad (Agent)", CHANNEL_NAGAD),
        ("নগদ পেমেন্ট", CHANNEL_NAGAD),
        ("Rocket Account", CHANNEL_ROCKET),
        ("DBBL Mobile Banking", CHANNEL_ROCKET),
        ("Upay wallet", CHANNEL_UPAY),
        ("Bank Transfer — A/C No", CHANNEL_BANK),
        ("ব্যাংক ট্রান্সফার", CHANNEL_BANK),
    ],
)
def test_brands_are_recognised(context, expected):
    assert classify_channel(context) == expected


def test_brand_beats_generic_bank_keyword():
    # A bKash block often also says "bank"; the specific brand must win.
    assert classify_channel("bKash — mobile banking deposit") == CHANNEL_BKASH


def test_ui_verb_tap_is_not_the_tap_wallet():
    assert classify_channel("Tap here to deposit") is None
    assert classify_channel("Tap to continue") is None


def test_no_channel_in_unrelated_text():
    assert classify_channel("Welcome to our site") is None
    assert classify_channel("") is None


def test_channel_near_assigns_by_proximity():
    text = "bKash 01711111111 ... some filler ... Nagad 01822222222"
    assert channel_near(text, text.index("01711111111")) == CHANNEL_BKASH
    assert channel_near(text, text.index("01822222222")) == CHANNEL_NAGAD


def test_channel_near_ignores_distant_mentions():
    text = "bKash" + " " * 900 + "01711111111"
    assert channel_near(text, text.index("01711111111")) is None


@pytest.mark.parametrize(
    "context,expected",
    [
        ("bKash Agent", "agent"),
        ("Nagad Merchant", "merchant"),
        ("Personal account", "personal"),
        ("Send Money only", "personal"),
        ("no type here", None),
    ],
)
def test_account_type(context, expected):
    assert classify_account_type(context) == expected
