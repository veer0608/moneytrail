"""Credit-card statements: a different identity, the same discipline.

There is no running balance to walk, so completeness is proved a different
way -- the transaction rows this parse recovered must add up to the totals the
issuer printed in its own summary box.
"""

from __future__ import annotations

import pytest

from moneytrail import CardStatement, Direction, parse_statement, reconcile_card
from moneytrail.cli import main
from moneytrail.insights import roll_up


def test_a_card_statement_is_recognised_as_one(card_path):
    statement = parse_statement(card_path)

    assert isinstance(statement, CardStatement)
    assert statement.issuer == "HDFC"
    assert statement.account_hint == "XXXXXXXXXXXX8918"


def test_bank_statements_are_not_dragged_onto_the_card_path(
    clean_statement_path, spreadsheet_path, pdf_path
):
    for path in (clean_statement_path, spreadsheet_path, pdf_path):
        assert not isinstance(parse_statement(path), CardStatement), path


def test_the_summary_box_is_read(card_path):
    summary = parse_statement(card_path).summary

    assert summary.previous_balance == 1_245_000
    assert summary.payments == 1_245_000
    assert summary.purchases == 825_050
    assert summary.fees == 7_000
    assert summary.total_due == 832_050
    assert summary.minimum_due == 41_600
    assert summary.due_date.isoformat() == "2025-08-05"


def test_cr_means_a_payment_not_a_purchase(card_path):
    # On a bank statement a credit is money in; on a card it is money off what
    # you owe. Reading the bank convention here would invert the whole ledger.
    payment = parse_statement(card_path).transactions[0]

    assert payment.direction is Direction.CREDIT
    assert payment.amount == 1_245_000
    assert "PAYMENT RECEIVED" in payment.narration


def test_rows_add_up_to_the_stated_totals(card_path):
    result = reconcile_card(parse_statement(card_path))

    assert result.ok, result.report()
    assert result.row_debits == 832_050  # purchases + finance charge
    assert result.row_credits == 1_245_000
    assert set(result.checks) == {"summary", "rows-debit", "rows-credit"}
    assert result.verified


def test_a_dropped_purchase_is_caught(card_path, tmp_path):
    lines = card_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if "NETFLIX" not in line]
    broken = tmp_path / "card.csv"
    broken.write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = reconcile_card(parse_statement(broken))

    assert not result.ok
    # The summary's own arithmetic still holds; only the row sum gives it away.
    kinds = {d.kind for d in result.discrepancies}
    assert kinds == {"rows-debit"}
    assert result.discrepancies[0].delta == -64_900  # the missing Rs 649.00


def test_a_statement_with_no_summary_says_it_is_unverified(tmp_path):
    target = tmp_path / "bare.csv"
    target.write_text(
        "Total Amount Due\n"
        "Date,Transaction Description,Amount\n"
        "03/07/2025,SOME PURCHASE,412.00\n",
        encoding="utf-8",
    )

    result = reconcile_card(parse_statement(target))

    assert result.checks == ()
    assert result.ok  # nothing failed, but nothing was proved either
    assert not result.verified
    assert "UNVERIFIED" in result.report()


def test_the_merchant_rollup_works_on_cards_unchanged(card_path):
    # The whole point of the card statement: it is where the merchant spend is.
    rollup = roll_up(parse_statement(card_path))

    names = {entry.name for entry in rollup.entries}
    assert {"Amazon", "Netflix", "Myntra", "BigBasket", "Indian Oil"} <= names
    assert rollup.by_kind["merchant"] >= 5


class TestCommand:
    def test_check_reports_a_card_statement(self, card_path, capsys):
        assert main(["check", str(card_path)]) == 0

        out = capsys.readouterr().out
        assert "total due" in out
        assert "RECONCILED to the paisa" in out


# --- a summary box read from where it sits ----------------------------------


def summary_page(lines):
    """Words laid out as a real card statement lays out its summary box."""
    def word(text, x0, top, width=None):
        return {
            "text": text,
            "x0": x0,
            "x1": x0 + (width if width is not None else len(text) * 4.0),
            "top": top,
        }
    return [[word(*w) for w in line] for line in lines]


def test_a_summary_box_is_read_by_position_not_by_columns():
    """A summary box is not a table and cannot be read as one.

    HDFC prints five headings across the top of a card statement and their five
    figures underneath, aligned by nothing but position. Split by the
    transaction table's columns -- which belong to a different table further
    down the document -- every value lands in the wrong cell.
    """
    from moneytrail.parsers.card import summary_patterns
    from moneytrail.parsers.words import read_labelled_values

    lines = summary_page([
        [("PAYMENTS/CREDITS", 155, 215.3, 60), ("PURCHASES/DEBIT", 258, 215.3, 55)],
        [("PREVIOUS", 39, 219.5, 30), ("STATEMENT", 71, 219.5, 35), ("DUES", 108, 219.5, 16),
         ("TOTAL", 445, 219.5, 19), ("AMOUNT", 466, 219.5, 28), ("DUE", 495, 219.5, 13)],
        [("RECEIVED", 155, 223.5, 33), ("(Current", 258, 223.5, 28)],
        [("₹", 445, 235.6, 8), ("19,375.00", 453, 235.6, 61)],
        [("₹", 63, 242.2, 4), ("15,447.95", 67, 242.2, 33),
         ("₹", 167, 242.2, 4), ("32,116.16", 171, 242.2, 33),
         ("+", 232, 242.2, 5),
         ("₹", 267, 242.2, 4), ("36,043.40", 271, 242.2, 33)],
    ])

    found = read_labelled_values(lines, summary_patterns())

    assert found["previous_balance"] == "₹ 15,447.95"
    assert found["payments"] == "₹ 32,116.16"
    assert found["purchases"] == "₹ 36,043.40"
    assert found["total_due"] == "₹ 19,375.00"


def test_a_heading_that_wraps_is_not_read_as_its_own_value():
    """"PAYMENTS/CREDITS" continues as "RECEIVED" on the line below it.

    Taking the first text underneath a label would read the label's own second
    half as its figure. Only an amount or a date counts as a value.
    """
    from moneytrail.parsers.card import summary_patterns
    from moneytrail.parsers.words import read_labelled_values

    lines = summary_page([
        [("PAYMENTS/CREDITS", 155, 100.0, 60)],
        [("RECEIVED", 155, 111.0, 33)],
        [("₹", 167, 122.0, 4), ("32,116.16", 171, 122.0, 33)],
    ])

    assert read_labelled_values(lines, summary_patterns())["payments"] == "₹ 32,116.16"


def test_a_value_too_far_below_its_label_is_not_claimed():
    """Another box lower down the page is not this label's figure."""
    from moneytrail.parsers.card import summary_patterns
    from moneytrail.parsers.words import read_labelled_values

    lines = summary_page([
        [("TOTAL AMOUNT DUE", 445, 100.0, 63)],
        [("₹", 445, 400.0, 8), ("19,375.00", 453, 400.0, 61)],
    ])

    assert "total_due" not in read_labelled_values(lines, summary_patterns())


# --- the issuer's own rounding ----------------------------------------------


def card_with(previous, payments, purchases, fees, total_due, rows):
    from pathlib import Path

    from moneytrail import CardStatement, CardSummary, Direction, Transaction
    from datetime import date

    return CardStatement(
        source=Path("card.pdf"),
        issuer="HDFC",
        account_hint="XXXX4880",
        summary=CardSummary(
            previous_balance=previous,
            payments=payments,
            purchases=purchases,
            fees=fees,
            total_due=total_due,
        ),
        transactions=tuple(
            Transaction(
                row=n,
                date=date(2026, 7, 1),
                narration="X",
                direction=d,
                amount=a,
            )
            for n, (d, a) in enumerate(rows, start=1)
        ),
    )


def test_the_issuers_own_rounding_is_a_note_not_a_failure():
    """HDFC bills a rounded total: 19,375.19 computed, 19,375.00 charged.

    Failing the statement over nineteen paise of the bank's own rounding would
    put a red verdict on every HDFC card statement, and a red verdict everyone
    learns to dismiss is worse than none. The rows still had to match exactly.
    """
    from moneytrail import Direction, reconcile_card

    statement = card_with(
        previous=1544795,
        payments=3211616,
        purchases=3604340,
        fees=0,
        total_due=1937500,  # computed is 1937519
        rows=[(Direction.DEBIT, 3604340), (Direction.CREDIT, 3211616)],
    )

    result = reconcile_card(statement)

    assert result.ok
    assert result.roundings
    assert "rounding" in result.roundings[0]


def test_a_gap_of_a_rupee_or_more_is_still_a_failure():
    """The tolerance is for rounding, not for a misread figure."""
    from moneytrail import Direction, reconcile_card

    statement = card_with(
        previous=1544795,
        payments=3211616,
        purchases=3604340,
        fees=0,
        total_due=1937419,  # a full rupee out
        rows=[(Direction.DEBIT, 3604340), (Direction.CREDIT, 3211616)],
    )

    assert not reconcile_card(statement).ok


def test_the_row_checks_are_never_tolerant():
    """They are the completeness proof.

    A tolerance here would let a missing transaction hide inside it, which is
    the one thing this whole project exists to catch.
    """
    from moneytrail import Direction, reconcile_card

    statement = card_with(
        previous=1544795,
        payments=3211616,
        purchases=3604340,
        fees=0,
        total_due=1937519,
        # One row short by fifty paise -- well inside the summary tolerance.
        rows=[(Direction.DEBIT, 3604290), (Direction.CREDIT, 3211616)],
    )

    result = reconcile_card(statement)

    assert not result.ok
    assert any(d.kind == "rows-debit" for d in result.discrepancies)
