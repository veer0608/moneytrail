"""The gate. If these fail, nothing built on top of the ledger can be trusted."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from moneytrail import (
    BalanceSource,
    Direction,
    Statement,
    Transaction,
    is_tautological,
    parse_statement,
    reconcile,
)

RUPEE = 100


def _txn(row, amount, direction=Direction.DEBIT, balance=None):
    return Transaction(
        row=row,
        date=date(2025, 4, row),
        narration=f"row {row}",
        direction=direction,
        amount=amount,
        balance=balance,
    )


def _statement(transactions, opening, closing):
    return Statement(
        source=Path("synthetic"),
        bank="test",
        account_hint="",
        opening_balance=opening,
        closing_balance=closing,
        transactions=tuple(transactions),
    )


def test_clean_statement_reconciles_to_the_paisa(clean_statement_path):
    result = reconcile(parse_statement(clean_statement_path))

    assert result.ok, result.report()
    assert result.discrepancies == ()
    assert result.chain_checked
    assert result.credits == 8_749_900  # Rs 87,499.00
    assert result.debits == 3_656_000  # Rs 36,560.00
    assert result.computed_closing == result.statement.closing_balance == 9_617_060


def test_dropped_row_is_caught_and_localised(dropped_row_path):
    result = reconcile(parse_statement(dropped_row_path))

    assert not result.ok
    kinds = {d.kind for d in result.discrepancies}
    assert kinds == {"chain", "totals"}

    # Both discrepancies are off by exactly the missing Netflix charge, and the
    # chain check points at the first row after the gap.
    assert all(d.delta == -649 * RUPEE for d in result.discrepancies)
    chain = next(d for d in result.discrepancies if d.kind == "chain")
    assert chain.row == result.first_bad_row == 10
    assert "MYNTRADESIGNS" in chain.narration


def test_a_misread_balance_is_an_equal_and_opposite_pair():
    # 20 rows, one wrong running balance in the middle. Because the walk resyncs
    # to the bank's own printed figure after a break, the signature of a single
    # bad *balance* is two adjacent faults that cancel -- not a cascade down the
    # rest of the statement, and distinguishable from a dropped row, which
    # leaves exactly one.
    transactions = []
    balance = 1_000_000
    for row in range(1, 21):
        balance -= 1_000
        transactions.append(_txn(row, 1_000, balance=balance))
    broken = transactions[9]
    transactions[9] = _txn(broken.row, broken.amount, balance=broken.balance + 5_000)

    result = reconcile(_statement(transactions, 1_000_000, balance))

    assert len(result.discrepancies) == 2  # 2 faults out of 20 rows, not 11
    assert [d.row for d in result.discrepancies] == [10, 11]
    assert [d.delta for d in result.discrepancies] == [5_000, -5_000]


def test_totals_are_checked_without_a_running_balance_column():
    transactions = [
        _txn(1, 50_000, Direction.DEBIT),
        _txn(2, 120_000, Direction.CREDIT),
    ]
    good = reconcile(_statement(transactions, 200_000, 270_000))
    assert good.ok
    assert not good.chain_checked

    bad = reconcile(_statement(transactions, 200_000, 270_001))
    assert not bad.ok
    assert [d.kind for d in bad.discrepancies] == ["totals"]


def test_derived_endpoints_are_flagged_as_a_weaker_check(signed_amount_path):
    statement = parse_statement(signed_amount_path)

    assert statement.opening_source is BalanceSource.DERIVED
    assert statement.closing_source is BalanceSource.DERIVED
    assert is_tautological(statement)
    assert reconcile(statement).ok


def test_explicit_endpoints_are_not_tautological(clean_statement_path):
    statement = parse_statement(clean_statement_path)

    assert statement.opening_source is BalanceSource.EXPLICIT
    assert statement.closing_source is BalanceSource.EXPLICIT
    assert not is_tautological(statement)


def test_report_names_the_failure(dropped_row_path):
    report = reconcile(parse_statement(dropped_row_path)).report()

    assert "FAILED" in report
    assert "₹649.00" in report or "-₹649.00" in report


@pytest.mark.parametrize("amount", [-1, -100])
def test_transaction_amount_must_be_a_magnitude(amount):
    with pytest.raises(ValueError, match="magnitude"):
        _txn(1, amount)
