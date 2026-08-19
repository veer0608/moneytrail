"""Which of ``03/04`` is the day, and why the file has to settle it.

This is the only reading the reconciliation gate cannot check. Dates play no
part in the arithmetic, so a statement read the wrong way round passes the
chain check, passes the totals check, and hands back a certificate saying
RECONCILED over a ledger where every date below the twelfth is wrong.
"""

from __future__ import annotations

import pytest

from moneytrail import parse_statement, reconcile
from moneytrail.parsers.base import (
    DateOrder,
    UnparseableStatement,
    infer_date_order,
    looks_like_date,
    parse_date,
)


# --- inference --------------------------------------------------------------


def test_a_day_above_twelve_settles_the_whole_file():
    """One such date decides it, and a month of transactions contains one."""
    order, observed = infer_date_order(["01/04/2025", "18/04/2025", "25/04/2025"])

    assert order is DateOrder.DAY_FIRST
    assert observed


def test_a_month_position_above_twelve_settles_it_the_other_way():
    order, observed = infer_date_order(["03/04/2025", "03/28/2025"])

    assert order is DateOrder.MONTH_FIRST
    assert observed


def test_chronology_decides_when_no_number_exceeds_twelve():
    """Statements run forwards, so only one reading puts these in order.

    Day-first reads 01/03, 02/02, 03/01 as 1 March, 2 February, 3 January --
    backwards. Month-first reads them as 3 January, 2 February, 1 March, which
    is the direction a statement actually runs.
    """
    order, observed = infer_date_order(["01/03/2025", "02/02/2025", "03/01/2025"])

    assert order is DateOrder.MONTH_FIRST
    assert observed


def test_chronology_can_settle_it_day_first_too():
    """The same signal, the other way round -- it is not biased to one answer."""
    order, observed = infer_date_order(["03/01/2025", "02/02/2025", "01/03/2025"])

    assert order is DateOrder.DAY_FIRST
    assert observed


def test_a_genuinely_undecidable_file_is_marked_assumed():
    """Every day at or below the twelfth, and in order either way.

    Nothing in the file settles it, so day-first is assumed -- and the
    certificate says "assumed" rather than pretending it was read.
    """
    order, observed = infer_date_order(["03/01/2025", "03/05/2025", "03/09/2025"])

    assert order is DateOrder.DAY_FIRST
    assert not observed


def test_a_file_that_contradicts_itself_is_refused():
    """One date above twelve in each position: no single order fits.

    Picking one would silently mangle half the ledger, so this raises.
    """
    with pytest.raises(UnparseableStatement, match="cannot be read consistently"):
        infer_date_order(["18/04/2025", "04/28/2025"])


def test_named_months_need_no_inference():
    order, observed = infer_date_order(["03 Apr 2025", "2025-04-03"])

    assert observed  # nothing ambiguous was there to get wrong


# --- parsing ----------------------------------------------------------------


def test_the_same_text_reads_differently_under_each_order():
    assert parse_date("03/04/2025", DateOrder.DAY_FIRST).month == 4
    assert parse_date("03/04/2025", DateOrder.MONTH_FIRST).month == 3


def test_unambiguous_formats_survive_either_order():
    for order in DateOrder:
        assert parse_date("2025-04-03", order).month == 4
        assert parse_date("03 Apr 2025", order).month == 4


def test_a_row_is_recognised_as_dated_under_either_reading():
    """Row detection happens before the order is known.

    Refusing to see 12/31/2025 as a date would drop the row entirely, and a
    dropped row is the failure this whole project exists to catch.
    """
    assert looks_like_date("12/31/2025")
    assert looks_like_date("31/12/2025")
    assert not looks_like_date("13/45/2025")


# --- end to end -------------------------------------------------------------


def test_a_us_statement_is_read_month_first(us_statement_path):
    statement = parse_statement(us_statement_path)

    assert statement.date_order == "month-first"
    assert statement.date_order_observed
    # 03/04/2025 is the fourth of March, not the third of April.
    assert statement.transactions[0].date.month == 3
    assert statement.transactions[0].date.day == 4


def test_the_us_statement_reconciles_too(us_statement_path):
    """The point being that it reconciled *before* the fix as well.

    Reading it day-first gave a ledger scattered across four months that still
    added up perfectly, because no check looks at a date. That is what made
    this worth building rather than leaving to a locale setting.
    """
    assert reconcile(parse_statement(us_statement_path)).ok


def test_indian_statements_are_unaffected(clean_statement_path):
    statement = parse_statement(clean_statement_path)

    assert statement.date_order == "day-first"
    assert statement.date_order_observed
    assert statement.transactions[0].date.month == 4


def test_the_certificate_reports_how_the_dates_were_read(us_statement_path):
    from moneytrail.export import certify

    text = certify(parse_statement(us_statement_path)).render()

    assert "dates read as   month-first" in text


def test_an_assumed_order_says_so(clean_statement_path, tmp_path):
    from moneytrail.export import certify

    short = tmp_path / "short.csv"
    short.write_text(
        "ACME BANK\n\n"
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "01/03/25,OPENING BALANCE,,,1000.00\n"
        "02/03/25,SOMETHING,100.00,,900.00\n"
        "03/03/25,CLOSING BALANCE,,,900.00\n",
        encoding="utf-8",
    )

    assert "assumed" in certify(parse_statement(short)).render()


# --- a real HDFC credit-card statement --------------------------------------


def test_a_date_carrying_a_time_still_parses():
    """HDFC's card statements stamp the time beside the date in one cell.

    ``22/06/2026| 23:14`` failing to parse loses the whole transaction, and
    nothing downstream is finer-grained than a day.
    """
    from datetime import date

    assert parse_date("22/06/2026| 23:14") == date(2026, 6, 22)
    assert parse_date("22/06/2026 | 23:14:07") == date(2026, 6, 22)
    assert parse_date("22/06/2026, 11:14 PM") == date(2026, 6, 22)
    assert looks_like_date("22/06/2026| 23:14")


def test_a_time_qualifier_on_the_header_still_names_the_date_column():
    from moneytrail.parsers.base import normalise_header

    assert normalise_header("DATE & TIME") == "date"
    assert normalise_header("Txn Date and Time") == "txn date"
    # A column genuinely called Time is not a date column wearing a qualifier.
    assert normalise_header("Time") == "time"
