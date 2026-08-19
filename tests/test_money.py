from __future__ import annotations

import pytest

from moneytrail import format_paise, parse_amount, parse_optional_amount


@pytest.mark.parametrize(
    ("text", "paise"),
    [
        ("412.00", 41_200),
        ("1,234.56", 123_456),
        ("1,23,456.78", 12_345_678),  # lakh grouping
        ("1,23,45,678.90", 1_234_567_890),  # crore grouping
        ("45231.60", 4_523_160),
        ("0.01", 1),
        ("7", 700),
        ("12.5", 1_250),  # one decimal place means tens of paise
        ("₹412", 41_200),
        ("Rs. 412.50", 41_250),
        ("INR 1,000.00", 100_000),
        ("1,234.00 Cr", 123_400),
        ("1,234.00 Dr", -123_400),
        ("(1,234.00)", -123_400),
        ("-1,234.00", -123_400),
    ],
)
def test_parse_amount(text, paise):
    assert parse_amount(text) == paise


@pytest.mark.parametrize("text", ["", "-", "--", "N/A", "  ", "nil"])
def test_blank_cells_are_absence_not_zero(text):
    assert parse_optional_amount(text) is None


@pytest.mark.parametrize("text", ["abc", "12.345", "1,2,3.4.5", "12 rupees", "--5"])
def test_unparseable_amounts_raise_rather_than_default_to_zero(text):
    with pytest.raises(ValueError):
        parse_amount(text)


@pytest.mark.parametrize(
    ("paise", "rendered"),
    [
        (41_200, "₹412.00"),
        (9_617_060, "₹96,170.60"),
        (12_345_678, "₹1,23,456.78"),
        (1_234_567_890, "₹1,23,45,678.90"),
        (-64_900, "-₹649.00"),
        (1, "₹0.01"),
    ],
)
def test_format_paise_uses_indian_grouping(paise, rendered):
    assert format_paise(paise) == rendered


def test_integer_paise_do_not_drift():
    # The float version of this is 0.9999999999999999.
    total = sum(parse_amount("0.10") for _ in range(10))
    assert total == parse_amount("1.00")


def test_a_sign_outside_the_currency_mark_is_still_a_sign():
    """HDFC's card statements write a payment as "+ ₹ 4,500.00".

    Stripping currency first and sign second leaves the plus attached, the
    amount fails to parse, and a real payment is lost from the ledger.
    """
    assert parse_amount("+ ₹ 4,500.00") == 450000
    assert parse_amount("₹-500.00") == -50000
    assert parse_amount("-₹99.00") == -9900
    assert parse_amount("(₹1,200.00)") == -120000


def test_two_signs_are_still_refused():
    """Peeling greedily would turn a corrupt cell into a confident number."""
    with pytest.raises(ValueError):
        parse_amount("--5")
    with pytest.raises(ValueError):
        parse_amount("+-5")
