"""Bank name and bank account extraction."""

from ght.normalize.bank import find_bank_accounts
from ght.normalize.banks_bd import bank_name_near, find_bank_name

DEPOSIT_BLOCK = """Bank Transfer
Islami Bank Bangladesh Ltd
A/C Name: Rahim Enterprise
A/C No: 2050 1234 5678 90123
Motijheel Branch
Also bKash 01712345678 and ticket 445566
"""


def test_extracts_account_with_full_context():
    hits = find_bank_accounts(DEPOSIT_BLOCK)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.account_number == "20501234567890123"
    assert hit.bank_name == "Islami Bank Bangladesh"
    assert hit.branch == "Motijheel"
    assert hit.holder_name == "Rahim Enterprise"


def test_mobile_number_is_not_taken_as_a_bank_account():
    numbers = [hit.account_number for hit in find_bank_accounts(DEPOSIT_BLOCK)]
    assert "01712345678" not in numbers


def test_digit_run_with_no_bank_nearby_is_dropped():
    # On a deposit page a bare long number is far more likely a transaction id.
    assert find_bank_accounts("Order id 998877665544 thanks") == []


def test_repeated_digits_are_rejected():
    assert find_bank_accounts("BRAC Bank 00000000000000") == []


def test_qualified_bank_names_are_not_confused_with_generic_ones():
    assert find_bank_name("Social Islami Bank Ltd") == "Social Islami Bank"
    assert find_bank_name("Al-Arafah Islami Bank") == "Al-Arafah Islami Bank"
    assert find_bank_name("Mutual Trust Bank Ltd") == "Mutual Trust Bank"
    # Bare "Islami Bank" with no qualifier is IBBL.
    assert find_bank_name("Islami Bank, Motijheel") == "Islami Bank Bangladesh"


def test_abbreviations_and_bangla_names():
    assert find_bank_name("Send to DBBL account") == "Dutch-Bangla Bank"
    assert find_bank_name("ইসলামী ব্যাংক শাখা") == "Islami Bank Bangladesh"
    assert find_bank_name("no bank mentioned") is None


def test_bank_name_near_picks_the_closest_bank():
    text = "Islami Bank 20501234567890123 ... BRAC Bank 15012034567890"
    assert bank_name_near(text, text.index("20501234567890123")) == "Islami Bank Bangladesh"
    assert bank_name_near(text, text.index("15012034567890")) == "BRAC Bank"


def test_bengali_number_label_is_not_read_as_a_holder_name():
    """"নাম" (name) is a prefix of "নাম্বার" (number); matching inside it yields garbage."""
    hits = find_bank_accounts("পূবালী ব্যাংক একাউন্ট নাম্বার 4536101001620")
    assert len(hits) == 1
    assert hits[0].account_number == "4536101001620"
    assert hits[0].bank_name == "Pubali Bank"
    assert hits[0].holder_name is None


def test_a_real_bengali_holder_label_still_matches():
    hits = find_bank_accounts("পূবালী ব্যাংক একাউন্ট নাম্বার 4536101001620 নাম: RIYA FASHION")
    assert hits[0].holder_name == "RIYA FASHION"


def test_a_row_of_preset_amounts_is_not_an_account():
    """The presets flatten into one long digit run inside the valid account length range."""
    text = "Please enter or select your deposit amount 1 000 2 000 5 000 7 000 10 000"
    assert find_bank_accounts(text) == []
