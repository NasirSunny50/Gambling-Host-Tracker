"""Gazetteer of Bangladeshi banks, used to attach a bank name to a bare account number.

Keys are the canonical name stored in the database; values are the aliases and
abbreviations these sites actually write. Matching is done on word boundaries, so
short abbreviations like ``EBL`` cannot fire inside an unrelated word.
"""

from __future__ import annotations

import re

BANK_ALIASES: dict[str, list[str]] = {
    # State-owned commercial banks
    "Sonali Bank": ["sonali", "সোনালী"],
    "Janata Bank": ["janata", "জনতা"],
    "Agrani Bank": ["agrani", "অগ্রণী"],
    "Rupali Bank": ["rupali", "রূপালী"],
    "BASIC Bank": ["basic bank"],
    # Specialised / development banks
    "Bangladesh Krishi Bank": ["krishi bank", "bkb", "কৃষি ব্যাংক"],
    "Probashi Kallyan Bank": ["probashi kallyan"],
    # Private commercial banks
    "Islami Bank Bangladesh": [
        "islami bank bangladesh",
        "ibbl",
        "ইসলামী ব্যাংক",
        # Bare "Islami Bank" is IBBL only when no other bank's qualifier precedes it.
        "(?<!security )(?<!social )(?<!shahjalal )(?<!arafah )(?<!global )islami bank",
    ],
    # Nexus is DBBL's card and wallet brand; deposit pages name the product, not the bank.
    "Dutch-Bangla Bank": [
        "dutch[- ]?bangla",
        "dbbl",
        "ডাচ[- ]?বাংলা",
        "nexus[- ]?pay",
        "নেক্সাস[- ]?পে",
    ],
    "BRAC Bank": ["brac bank", "ব্র্যাক ব্যাংক"],
    "The City Bank": ["city bank", "সিটি ব্যাংক"],
    "Eastern Bank": ["eastern bank", "ebl"],
    "Prime Bank": ["prime bank", "প্রাইম"],
    "Southeast Bank": ["southeast bank", "সাউথইস্ট"],
    "Dhaka Bank": ["dhaka bank", "ঢাকা ব্যাংক"],
    "Mercantile Bank": ["mercantile"],
    "National Bank": ["national bank", "nbl"],
    "Pubali Bank": ["pubali", "পূবালী"],
    "Uttara Bank": ["uttara bank", "উত্তরা ব্যাংক"],
    "AB Bank": ["ab bank", "arab bangladesh bank"],
    "IFIC Bank": ["ific"],
    "ONE Bank": ["one bank"],
    "Bank Asia": ["bank asia", "ব্যাংক এশিয়া"],
    "The Premier Bank": ["premier bank"],
    "Trust Bank": ["trust bank", "ট্রাস্ট ব্যাংক"],
    "Jamuna Bank": ["jamuna bank", "যমুনা"],
    "Shahjalal Islami Bank": ["shahjalal", "শাহজালাল"],
    "Al-Arafah Islami Bank": ["al[- ]?arafah", "আল[- ]?আরাফাহ"],
    "Social Islami Bank": ["social islami", "sibl"],
    "EXIM Bank": ["exim bank"],
    "First Security Islami Bank": ["first security", "fsibl"],
    "Union Bank": ["union bank"],
    "Standard Bank": ["standard bank"],
    "NRB Bank": ["nrb bank"],
    "NRB Commercial Bank": ["nrb commercial", "nrbc"],
    "Modhumoti Bank": ["modhumoti"],
    "Midland Bank": ["midland bank"],
    "Meghna Bank": ["meghna bank"],
    "South Bangla Agriculture and Commerce Bank": ["south bangla", "sbac"],
    "Padma Bank": ["padma bank"],
    "Community Bank Bangladesh": ["community bank"],
    "Bengal Commercial Bank": ["bengal commercial"],
    "Citizens Bank": ["citizens bank"],
    "Global Islami Bank": ["global islami"],
    "United Commercial Bank": ["united commercial", "ucb"],
    "Mutual Trust Bank": ["mutual trust", "mtb"],
    # Foreign banks operating in Bangladesh
    "Standard Chartered Bangladesh": ["standard chartered", "scb"],
    "HSBC Bangladesh": ["hsbc"],
    "Citibank N.A.": ["citibank", r"citi n\.?a\.?"],
    "Commercial Bank of Ceylon": ["commercial bank of ceylon"],
    "State Bank of India": ["state bank of india", "sbi"],
    "Woori Bank": ["woori"],
    "Habib Bank": ["habib bank"],
}

# Each bank keeps its own pattern; overlaps between them are resolved after matching so
# that "Mutual Trust Bank" is never also reported as "Trust Bank".
_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (canonical, re.compile(r"(?<!\w)(?:" + "|".join(aliases) + r")(?!\w)", re.IGNORECASE))
    for canonical, aliases in BANK_ALIASES.items()
]


def _resolve_matches(text: str) -> list[tuple[int, int, str]]:
    """Return non-overlapping ``(start, end, canonical)`` bank mentions, longest match wins."""
    candidates: list[tuple[int, int, str]] = []
    for canonical, pattern in _COMPILED:
        for match in pattern.finditer(text):
            candidates.append((match.start(), match.end(), canonical))

    # Earliest start first, and at the same start the longest span first, so a greedy
    # sweep keeps "Mutual Trust Bank" and discards the "Trust Bank" nested inside it.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    resolved: list[tuple[int, int, str]] = []
    consumed_to = -1
    for start, end, canonical in candidates:
        if start >= consumed_to:
            resolved.append((start, end, canonical))
            consumed_to = end
    return resolved


def find_bank_name(text: str) -> str | None:
    """Return the canonical name of the first bank mentioned in ``text``."""
    if not text:
        return None
    matches = _resolve_matches(text)
    return matches[0][2] if matches else None


def bank_name_near(text: str, position: int, window: int = 400) -> str | None:
    """Return the bank name mentioned closest to ``position``, within ``window`` chars."""
    if not text:
        return None
    best_name: str | None = None
    best_distance = window + 1
    for start, end, canonical in _resolve_matches(text):
        if end <= position:
            distance = position - end
        elif start >= position:
            distance = start - position
        else:
            distance = 0
        if distance < best_distance:
            best_distance = distance
            best_name = canonical
    return best_name
