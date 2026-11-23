import pytest

from app.services.names import name_key, name_words
from app.services.phones import mask_phone, normalise_phone


# ---- phones ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "71234567", "26771234567", "+26771234567", "0026771234567",
    "71 234 567", "71-234-567", "(71) 234 567", "+267 71 234 567", " 71234567 ", "+267-7123-4567",
])
def test_botswana_mobile_formats_normalise(raw):
    r = normalise_phone(raw)
    assert r.value == "+26771234567" and not r.error and not r.warnings


def test_blank_phone_is_not_an_error():
    r = normalise_phone("  ")
    assert r.value is None and r.error is None


@pytest.mark.parametrize("raw", ["+27821234567", "0027821234567", "+447911123456", "+1 (415) 555-2671"])
def test_other_countries_accepted_with_plus_or_00(raw):
    r = normalise_phone(raw)
    assert r.value and r.value.startswith("+") and not r.error


@pytest.mark.parametrize("raw", [
    "7123456",          # 7 digits
    "0712345678",       # 10 digits with a leading 0
    "27821234567",      # other country without +
    "abc12345",
    "+267712345678",    # Botswana with 9 digits
    "+2677123456",      # Botswana with 7 digits
    "+12345",           # too short
    "+1234567890123456",  # too long (16 digits)
    "+0123456789",      # country code can't start with 0
    "7123-45x67",
])
def test_invalid_phones_are_errors(raw):
    r = normalise_phone(raw)
    assert r.value is None and r.error


@pytest.mark.parametrize("raw", ["31234567", "+26731234567", "26721234567"])
def test_botswana_non_mobile_pattern_is_accepted_with_warning(raw):
    r = normalise_phone(raw)
    assert r.value and r.value.startswith("+267") and r.warnings and not r.error


def test_mask_phone_shows_last_three_digits_only():
    assert mask_phone("+26771234567") == "***567" and mask_phone(None) == "-"


# ---- names -----------------------------------------------------------------------------------------------------

def test_name_key_ignores_order_case_accents_and_punctuation():
    assert name_key("Thabo", "Molefe") == name_key("MOLEFE", "thabo") == "molefe thabo"
    assert name_key("Zoë", "O'Brien") == name_key("zoe", "obrien")
    assert name_words("thabo_molefe-2024.pdf") == ["thabo", "molefe", "pdf"]
