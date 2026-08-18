"""Bank account number extraction.

Unlike MSISDNs, BD bank account numbers have no single fixed shape — 13 digits at DBBL,
17 at BRAC, 20 at IBBL. So the number itself carries almost no signal; what makes a digit
run a bank account is the bank name, branch and holder name sitting around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ght.normalize.banks_bd import bank_name_near
from ght.normalize.msisdn import normalize_msisdn, translate_digits

# A digit run long enough to be an account number, tolerating spaces and dashes inside.
# Bounded by non-digits so we take the whole run and can then judge its true length.
_DIGIT_RUN = re.compile(r"(?<![\d])\d[\d\s\-]{7,26}\d(?![\d])")

MIN_ACCOUNT_DIGITS = 9
MAX_ACCOUNT_DIGITS = 20

_BRANCH = re.compile(
    r"([A-Za-z\u0980-\u09FF][A-Za-z\u0980-\u09FF \t.\-]{1,30}?)[ \t]*(?:branch|শাখা)",
    re.IGNORECASE,
)
_HOLDER = re.compile(
    # "নাম" (name) is a prefix of "নাম্বার" (number), and Bengali gives the regex no word
    # boundary to stop at - without the lookahead this matches inside every "wallet number"
    # label on the page and captures the fragment left over.
    r"(?:a/?c\s*(?:name|holder)|(?:account|wallet)\s*(?:name|holder)|holder\s*name"
    r"|নাম(?!্বার))"
    # "M/S" (Messrs) prefixes a great many Bangladeshi business names, and "&" is common
    # in them too. Without these the whole name fails to match and the search falls
    # through to whatever label sits further down the page.
    r"[ \t]*[:\-]?[ \t]*([A-Za-z\u0980-\u09FF][A-Za-z\u0980-\u09FF \t.\-/&]{2,40})",
    re.IGNORECASE,
)


# Labels that mark a long digit run as something other than an account: deposit pages are
# full of transaction ids and reference numbers sitting near the real account details.
_NOT_AN_ACCOUNT_LABEL = re.compile(
    r"\b(?:ticket|transaction|trx|txn|reference|ref|order|invoice|receipt"
    # A row of preset deposit amounts flattens into a single long digit run once the
    # spaces between them are swallowed, landing squarely inside the account-length range.
    r"|amount|bdt|min|max)\b[^\d]{0,20}$",
    re.IGNORECASE,
)
_LABEL_LOOKBACK = 40

_BRANCH_WORDS = {"branch", "শাখা"}


@dataclass(frozen=True)
class BankAccountHit:
    account_number: str
    bank_name: str | None
    branch: str | None
    holder_name: str | None
    position: int


def _clean_branch(value: str | None) -> str | None:
    """Drop the word "branch" itself, which the surrounding label repeats."""
    if not value:
        return None
    words = [word for word in value.split() if word.lower() not in _BRANCH_WORDS]
    return " ".join(words[-3:]) or None


def holder_name_in(text: str) -> str | None:
    """The account holder named by a label in ``text``, if one is labelled at all."""
    match = _HOLDER.search(text or "")
    return _clean_holder(match.group(1).strip()) if match else None


# Words that belong to the page furniture rather than to anybody's name. The value sits
# in a flattened run of text, so the copy button's label and the next field's caption
# follow the name directly and would otherwise be read as part of it.
_HOLDER_STOP_WORDS = frozenset(
    {
        "copied",
        "copy",
        "amount",
        "min",
        "max",
        "bdt",
        "transaction",
        "reference",
        "please",
        "enter",
        "code",
        # The label of whatever field follows the name, which runs straight into it.
        "a/c",
        "ac",
        "account",
        "no",
        "number",
        "branch",
    }
)


def _clean_holder(value: str | None) -> str | None:
    """Cut the captured name back to the name itself."""
    if not value:
        return None
    words: list[str] = []
    for word in value.split():
        if word.lower().strip(".,:;") in _HOLDER_STOP_WORDS:
            break
        words.append(word)
    # Trim a trailing initial left behind by the next label ("... Enterprise A/C No").
    while words and len(words[-1]) == 1:
        words.pop()
    return " ".join(words) or None


def _looks_like_account(digits: str) -> bool:
    """Reject digit runs that are long but obviously not account numbers."""
    if not MIN_ACCOUNT_DIGITS <= len(digits) <= MAX_ACCOUNT_DIGITS:
        return False
    if len(set(digits)) == 1:  # 00000000000, 11111111111
        return False
    # A BD mobile number is not a bank account, even though it is 11 digits long.
    return normalize_msisdn(digits) is None


def _nearest(pattern: re.Pattern[str], text: str, position: int, window: int) -> str | None:
    """Return group 1 of the match closest to ``position``, within ``window`` chars."""
    best: str | None = None
    best_distance = window + 1
    for match in pattern.finditer(text):
        if match.end() <= position:
            distance = position - match.end()
        elif match.start() >= position:
            distance = match.start() - position
        else:
            distance = 0
        if distance < best_distance:
            best_distance = distance
            best = match.group(1).strip(" .-")
    return best


def find_bank_accounts(
    text: str, window: int = 400, require_bank: bool = True
) -> list[BankAccountHit]:
    """Find bank account numbers in free text, with the context that identifies them.

    A digit run with no bank name anywhere near it is dropped — on a gambling deposit page
    that is far more likely to be a transaction id or a ticket number than an account.

    Pass ``require_bank=False`` when the caller already knows the bank from config. Every
    other guard still applies: those are what separate an account number from the amount
    presets and reference ids sitting in the same panel.
    """
    if not text:
        return []

    translated = translate_digits(text)
    hits: list[BankAccountHit] = []
    seen: set[str] = set()

    for match in _DIGIT_RUN.finditer(translated):
        digits = re.sub(r"\D", "", match.group(0))
        if not _looks_like_account(digits) or digits in seen:
            continue

        bank_name = bank_name_near(translated, match.start(), window)
        if bank_name is None and require_bank:
            continue

        # "Reference ticket id 998877665544" sits inside the same bank block but is not an
        # account, so check what the number is actually labelled as.
        preceding = translated[max(0, match.start() - _LABEL_LOOKBACK) : match.start()]
        if _NOT_AN_ACCOUNT_LABEL.search(preceding):
            continue

        seen.add(digits)
        hits.append(
            BankAccountHit(
                account_number=digits,
                bank_name=bank_name,
                branch=_clean_branch(_nearest(_BRANCH, translated, match.start(), window)),
                holder_name=_clean_holder(_nearest(_HOLDER, translated, match.start(), window)),
                position=match.start(),
            )
        )
    return hits
