from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from moneytrail import Direction, parse_statement
from moneytrail.parsers import NoParserFound, UnparseableStatement


def test_reads_preamble_metadata(clean_statement_path):
    statement = parse_statement(clean_statement_path)

    assert statement.bank == "HDFC"
    assert statement.account_hint == "XXXXXXXX4471"
    assert statement.period_start == date(2025, 4, 1)
    assert statement.period_end == date(2025, 4, 25)


def test_balance_marker_rows_become_endpoints_not_transactions(clean_statement_path):
    statement = parse_statement(clean_statement_path)

    assert len(statement.transactions) == 7
    assert statement.opening_balance == 4_523_160
    assert statement.closing_balance == 9_617_060
    assert all("BALANCE" not in t.narration for t in statement.transactions)


def test_debit_and_credit_columns_map_to_direction(clean_statement_path):
    statement = parse_statement(clean_statement_path)
    first, salary = statement.transactions[0], statement.transactions[1]

    assert first.direction is Direction.DEBIT
    assert first.amount == 41_200
    assert first.signed == -41_200
    assert first.value_date == date(2025, 4, 1)
    assert "SWIGGYINSTAMART" in first.narration

    assert salary.direction is Direction.CREDIT
    assert salary.signed == 8_500_000


def test_row_numbers_point_back_at_the_source_line(clean_statement_path):
    statement = parse_statement(clean_statement_path)

    # Header is line 6, opening-balance marker line 7, first transaction line 8.
    assert statement.transactions[0].row == 8
    assert [t.row for t in statement.transactions] == list(range(8, 15))


def test_single_signed_amount_column(signed_amount_path):
    statement = parse_statement(signed_amount_path)

    assert statement.bank == "ICICI"
    assert statement.account_hint == "****8820"
    assert [t.direction for t in statement.transactions] == [
        Direction.DEBIT,
        Direction.DEBIT,
        Direction.CREDIT,
    ]
    assert statement.transactions[0].amount == 32_150
    assert statement.opening_balance == 1_232_150  # derived from row 1


def test_unknown_file_type_is_rejected(tmp_path: Path):
    target = tmp_path / "statement.docx"
    target.write_bytes(b"not a statement")

    with pytest.raises(NoParserFound):
        parse_statement(target)


def test_a_table_with_no_recognisable_header_is_rejected(tmp_path: Path):
    target = tmp_path / "mystery.csv"
    target.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    with pytest.raises(NoParserFound):
        parse_statement(target)


def test_a_row_that_is_neither_amount_nor_marker_is_loud(tmp_path: Path):
    target = tmp_path / "odd.csv"
    target.write_text(
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/04/25,OPENING BALANCE,,,1000.00\n"
        "02/04/25,SOMETHING WITH NO AMOUNT,,,900.00\n",
        encoding="utf-8",
    )

    with pytest.raises(UnparseableStatement, match="no debit or credit"):
        parse_statement(target)
