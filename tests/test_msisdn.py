"""MSISDN normalization — the shapes these numbers actually appear in on BD sites."""

import pytest

from ght.normalize.msisdn import find_msisdns, normalize_msisdn, operator_of

CANONICAL = "+8801712345678"


@pytest.mark.parametrize(
    "raw",
    [
        "01712345678",  # plain national
        "+8801712345678",  # E.164
        "8801712345678",  # country code, no plus
        "1712345678",  # trunk zero dropped
        "01712-345678",  # dashed
        "017 1234 5678",  # spaced
        "017.1234.5678",  # dotted
        "+880 1712-345678",  # mixed
        "০১৭১২৩৪৫৬৭৮",  # Bangla numerals
        "৮৮০১৭১২৩৪৫৬৭৮",  # Bangla numerals with country code
        "০১৭১২-৩৪৫৬৭৮",  # Bangla numerals, dashed
    ],
)
def test_variants_all_normalize_to_one_value(raw):
    assert normalize_msisdn(raw) == CANONICAL


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "01212345678",  # 012 is not an allocated mobile prefix
        "0171234567",  # one digit short
        "017123456789",  # one digit long
        "20501234567890123",  # bank account number
        "not a number",
        "445566",
    ],
)
def test_invalid_inputs_are_rejected(raw):
    assert normalize_msisdn(raw) is None


def test_all_allocated_operator_prefixes_are_valid():
    for prefix in ("013", "014", "015", "016", "017", "018", "019"):
        assert normalize_msisdn(f"{prefix}12345678") is not None


def test_find_msisdns_extracts_each_number_once_in_order():
    text = "bKash 017 1234 5678 / Nagad ০১৮১২৩৪৫৬৭৮ / bKash again 01712345678"
    assert find_msisdns(text) == [CANONICAL, "+8801812345678"]


def test_find_msisdns_does_not_bite_into_a_longer_digit_run():
    # The account number contains a valid-looking 11-digit substring; it must not match.
    assert find_msisdns("A/C 12345601712345678 only") == []


def test_find_msisdns_separates_adjacent_numbers():
    assert len(find_msisdns("01712345678 01812345678")) == 2


def test_operator_lookup():
    assert operator_of("01712345678") == "Grameenphone"
    assert operator_of("01912345678") == "Banglalink"
    assert operator_of("01512345678") == "Teletalk"
    assert operator_of("01612345678") == "Robi"
    assert operator_of("nonsense") is None
