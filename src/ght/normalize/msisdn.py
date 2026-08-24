"""Bangladeshi mobile number (MSISDN) detection and normalization.

Gambling sites publish deposit numbers in every imaginable shape:
``01712345678``, ``+880 1712-345678``, ``৮৮০১৭১২৩৪৫৬৭৮`` (Bangla numerals).
Everything here funnels those into one canonical form: ``+8801XXXXXXXXX``.
"""

from __future__ import annotations

import re

# Bangla (Bengali) numerals appear on a large share of BD-facing sites.
BANGLA_DIGITS = "০১২৩৪৫৬৭৮৯"
_DIGIT_TRANSLATION = str.maketrans(BANGLA_DIGITS, "0123456789")

# A BD mobile number is 11 national digits: 0, 1, operator digit (3-9), 8 subscriber digits.
# The lookarounds stop us from biting a chunk out of a longer digit run such as a
# 17-digit bank account number.
MSISDN_PATTERN = re.compile(
    r"""(?<![0-9])            # not continuing a longer digit run
        (?:\+?88[\s\-.]?)?    # optional country code, written +88 / 88
        0?1[3-9]              # national trunk 0 (optional) + operator digit
        (?:[\s\-.]?\d){8}     # 8 subscriber digits, single separators tolerated
        (?![0-9])
    """,
    re.VERBOSE,
)

_NATIONAL_PATTERN = re.compile(r"^01[3-9]\d{8}$")

# Operator is not the same thing as the payment channel — a bKash wallet can sit on any
# operator's number. This is recorded for analysis, never used to infer the channel.
OPERATOR_PREFIXES = {
    "013": "Grameenphone",
    "017": "Grameenphone",
    "014": "Banglalink",
    "019": "Banglalink",
    "015": "Teletalk",
    "016": "Robi",
    "018": "Robi",
}


def translate_digits(text: str) -> str:
    """Convert Bangla numerals to ASCII so a single regex handles both scripts."""
    return text.translate(_DIGIT_TRANSLATION)


def normalize_msisdn(raw: str) -> str | None:
    """Return ``+8801XXXXXXXXX`` for anything that resolves to a valid BD mobile number.

    Returns ``None`` when the input is not a BD mobile number, so callers can use this
    as a validity check as well as a formatter.
    """
    if not raw:
        return None

    digits = re.sub(r"\D", "", translate_digits(raw))

    if len(digits) == 13 and digits.startswith("880"):
        national = digits[2:]
    elif len(digits) == 12 and digits.startswith("88"):
        national = "0" + digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        national = digits
    elif len(digits) == 10 and digits.startswith("1"):
        national = "0" + digits
    else:
        return None

    if not _NATIONAL_PATTERN.match(national):
        return None
    return "+88" + national


def find_msisdns(text: str) -> list[str]:
    """Find every BD mobile number in free text, normalized and de-duplicated.

    Order is preserved so the first sighting on a page stays first.
    """
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()
    for match in MSISDN_PATTERN.finditer(translate_digits(text)):
        normalized = normalize_msisdn(match.group(0))
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


# Rocket (Dutch-Bangla) writes a wallet as the holder's mobile number with a check digit
# on the end - twelve digits where every other MFS shows eleven. The mobile is the
# identity: the twelfth is derived from it, so two Rocket wallets cannot differ only there.
_ROCKET_PATTERN = re.compile(
    r"""(?<![0-9])
        (?:\+?88[\s\-.]?)?
        0?1[3-9]
        (?:[\s\-.]?\d){9}   # one more than a plain MSISDN: the check digit
        (?![0-9])
    """,
    re.VERBOSE,
)


def _rocket_national(raw: str) -> str | None:
    """The twelve national digits of a Rocket wallet — ``0`` + mobile + check — or None.

    The first eleven must themselves be a valid mobile, which is what tells a Rocket wallet
    apart from a bank account that happens to run to twelve digits.
    """
    if not raw:
        return None
    match = _ROCKET_PATTERN.search(translate_digits(raw))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    # Strip a country code before counting: +8801XXXXXXXXXC is the same wallet.
    if digits.startswith("880"):
        digits = "0" + digits[3:]
    elif digits.startswith("88"):
        digits = "0" + digits[2:]
    if len(digits) != 12 or normalize_msisdn(digits[:11]) is None:
        return None
    return digits


def normalize_rocket(raw: str) -> str | None:
    """The full 12-digit Rocket wallet in canonical form, or ``None`` if it is not one.

    Rocket (Dutch-Bangla) publishes the wallet as the holder's mobile with a check digit
    appended — twelve digits where every other MFS shows eleven. The published number *is*
    the account, so all twelve are kept: a blocklist has to carry what the site prints, not
    a truncation of it. The form is the same ``+880…`` shape as every other channel, with
    the trunk ``0`` replaced by the country code, so ``018046326747`` is keyed as
    ``+88018046326747``. Two wallets still cannot collide on it, because the twelfth digit
    is derived from the first eleven rather than free to vary.
    """
    digits = _rocket_national(raw)
    if digits is None:
        return None
    return "+880" + digits[1:]


def operator_of(msisdn: str) -> str | None:
    """Map a normalized number to its mobile operator, or ``None`` if unrecognised."""
    normalized = normalize_msisdn(msisdn)
    if normalized is None:
        # A Rocket wallet is a mobile with a check digit past it, so it never parses as an
        # MSISDN — but its operator is still the mobile's, read off the same prefix.
        national = _rocket_national(msisdn)
        normalized = normalize_msisdn(national[:11]) if national else None
    if normalized is None:
        return None
    return OPERATOR_PREFIXES.get(normalized[3:6])
