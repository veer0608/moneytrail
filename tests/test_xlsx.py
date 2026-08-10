"""Every test here exists because a real bank export broke the parser.

The synthetic CSV and PDF fixtures were too clean to catch any of it: a
workbook named .xls, asterisk divider rows, masked amounts, and a summary
block at the foot whose labels and values live on separate rows.
"""

from __future__ import annotations

import pytest

from moneytrail import BalanceSource, parse_statement, reconcile
from moneytrail.money import parse_optional_amount
from moneytrail.parsers import UnparseableStatement


def test_a_workbook_named_xls_is_read_anyway(spreadsheet_path):
    # openpyxl refuses .xls on the extension alone; banks ship OOXML with that
    # name constantly, so the magic bytes decide instead.
    assert spreadsheet_path.suffix == ".xls"
    assert reconcile(parse_statement(spreadsheet_path)).ok


def test_divider_rows_are_not_transactions(spreadsheet_path):
    statement = parse_statement(spreadsheet_path)

    assert len(statement.transactions) == 7
    assert all("*" not in t.narration for t in statement.transactions)


def test_the_summary_block_supplies_a_stated_closing_balance(spreadsheet_path):
    statement = parse_statement(spreadsheet_path)

    # Not derived from the last row -- read from the bank's own summary table,
    # which is what makes the totals check a comparison rather than a tautology.
    assert statement.closing_source is BalanceSource.EXPLICIT
    assert statement.closing_balance == 9_617_060


def test_footer_rows_after_the_summary_are_ignored(spreadsheet_path):
    statement = parse_statement(spreadsheet_path)

    narrations = " ".join(t.narration for t in statement.transactions)
    assert "Generated On" not in narrations
    assert "Dr Count" not in narrations


def test_all_three_formats_agree(spreadsheet_path, clean_statement_path, pdf_path):
    def ledger(path):
        return [
            (t.date, t.narration, t.direction, t.amount, t.balance)
            for t in parse_statement(path).transactions
        ]

    assert ledger(spreadsheet_path) == ledger(clean_statement_path) == ledger(pdf_path)


@pytest.mark.parametrize("text", ["******************", "####", "-----", "***"])
def test_masked_and_overflowed_cells_are_absence_not_zero(text):
    assert parse_optional_amount(text) is None


def test_a_legacy_binary_xls_says_what_to_do(tmp_path):
    legacy = tmp_path / "old.xls"
    legacy.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 64)

    with pytest.raises(UnparseableStatement, match="re-save as .xlsx or CSV"):
        parse_statement(legacy)
