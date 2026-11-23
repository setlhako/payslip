import pytest

from app.services.ids import parse_id


def test_long_omang_keeps_only_last_four():
    r = parse_id("123456789", "omang")
    assert (r.id_type, r.number, r.last4) == ("omang", "123456789", "6789") and not r.warnings and not r.error


def test_four_digit_omang_keeps_last4_but_warns_it_is_not_a_full_number():
    r = parse_id("0042", "omang")
    assert r.last4 == "0042" and r.number == "0042" and any("9 digits" in w for w in r.warnings) and not r.error


@pytest.mark.parametrize("raw,expected", [("123", "0123"), ("7", "0007"), ("42", "0042")])
def test_short_omang_padded_with_warning(raw, expected):
    r = parse_id(raw, "omang")
    assert r.last4 == expected and r.number == expected and r.warnings and not r.error   # the stored number is the padded one


@pytest.mark.parametrize("raw", ["1.23E+8", "12,345", "12_345", "12#4567"])
def test_bad_characters_are_errors_and_the_value_is_never_repeated(raw):
    for kind in ("omang", "passport", None):
        r = parse_id(raw, kind)
        assert r.last4 is None and r.error and raw not in r.error


def test_omang_cannot_contain_letters():
    r = parse_id("12AB34567", "omang")
    assert r.error and "passport" in r.error


def test_passport_number_and_last4_are_uppercase():
    r = parse_id("ab12cd34", "passport")
    assert (r.number, r.last4) == ("AB12CD34", "CD34") and not r.warnings
    assert parse_id("Ab1234567", "passport").last4 == "4567"


@pytest.mark.parametrize("raw", ["AB1", "A123", "AB12", "A" * 16])
def test_passport_outside_5_to_15_characters_is_an_error(raw):
    r = parse_id(raw, "passport")
    assert r.last4 is None and r.number is None and "5 to 15" in r.error and raw not in r.error


@pytest.mark.parametrize("raw,warns", [("AB123", True), ("AB1234", False), ("AB1234567", False), ("AB12345678", True), ("A" * 15, True)])
def test_passport_length_outside_6_to_9_warns_but_is_accepted(raw, warns):
    r = parse_id(raw, "passport")
    assert not r.error and r.number == raw and bool(r.warnings) is warns


@pytest.mark.parametrize("raw,warns", [("123456789", False), ("12345678", True), ("1234567890", True), ("1234", True)])
def test_omang_that_is_not_9_digits_warns_but_is_accepted(raw, warns):
    r = parse_id(raw, "omang")
    assert not r.error and r.number == raw and bool(r.warnings) is warns


def test_all_digit_passport_is_allowed_when_type_is_explicit():
    r = parse_id("12345678", "passport")
    assert (r.id_type, r.number, r.last4) == ("passport", "12345678", "5678") and not r.warnings


@pytest.mark.parametrize("raw,number,last4", [("123 456-789", "123456789", "6789"), (" 1234 ", "1234", "1234")])
def test_spaces_and_dashes_ignored(raw, number, last4):
    r = parse_id(raw, "omang")
    assert (r.number, r.last4) == (number, last4)


def test_guessing_when_type_is_blank():
    omang, passport = parse_id("123456789", ""), parse_id("AB1234567", None)
    assert (omang.id_type, omang.last4) == ("omang", "6789") and omang.warnings
    assert (passport.id_type, passport.last4) == ("passport", "4567") and passport.warnings


def test_blank_number_is_not_an_error():
    r = parse_id("  ", None)
    assert r.last4 is None and r.number is None and r.id_type is None and not r.error and not r.warnings


def test_unknown_type_is_an_error():
    assert parse_id("123456789", "visa").error


def test_the_number_never_appears_in_warnings_or_errors():
    for raw, kind in [("12345678", "omang"), ("AB123", "passport"), ("12", "omang"), ("12#4", "omang"), ("AB1", "passport")]:
        r = parse_id(raw, kind)
        assert raw not in " ".join(r.warnings + [r.error or ""])
