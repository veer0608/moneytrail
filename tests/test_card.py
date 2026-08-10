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
