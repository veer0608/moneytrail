"""Recovering a grid from where the words sit, for statements that draw none."""

from __future__ import annotations

import pytest

from moneytrail.parsers.words import (
    LABEL_GAP,
    LINE_TOLERANCE,
    label_threshold,
    assemble,
    cluster_lines,
    column_map,
    group_labels,
    line_to_cells,
    place,
    read_header,
)


def word(text: str, x0: float, x1: float = None, top: float = 100.0) -> dict:
    """One extracted word, positioned as pdfplumber reports one."""
    return {
        "text": text,
        "x0": x0,
        "x1": x1 if x1 is not None else x0 + len(text) * 4.5,
        "top": top,
    }


def at(top: float, header):
    """The same words moved to another line."""
    return [word(w["text"], w["x0"], w["x1"], top=top) for w in header]


#: A real HDFC header, at the extents pdfplumber actually reports for it.
#: Word spaces measure 1.8pt here and the narrowest column gap 12.7pt, which is
#: the distribution `label_threshold` exists to separate. Invented widths would
#: make these tests agree with an imagined statement instead of a real one.
HDFC_HEADER = [
    word("Date", 40.0, 53.7),
    word("Narration", 80.0, 106.7),
    word("Chq./Ref.No.", 200.0, 237.6),
    word("Value", 290.0, 306.6), word("Dt", 308.4, 314.9),
    word("Withdrawal", 345.0, 377.1), word("Amt.", 378.9, 392.3),
    word("Deposit", 405.0, 427.0), word("Amt.", 428.8, 442.2),
    word("Closing", 465.0, 486.7), word("Balance", 488.5, 512.0),
]


# --- lines ------------------------------------------------------------------


def test_words_on_the_same_line_are_grouped():
    lines = cluster_lines([word("b", 80, top=100), word("a", 40, top=100.5)])

    assert len(lines) == 1
    assert [w["text"] for w in lines[0]] == ["a", "b"]  # sorted left to right


def test_the_line_below_is_not_swallowed():
    """Statements set 8-10pt type on ~11pt leading; the tolerance must not reach."""
    lines = cluster_lines([word("a", 40, top=100), word("b", 40, top=100 + 11)])

    assert len(lines) == 2
    assert LINE_TOLERANCE < 11


# --- headings ---------------------------------------------------------------


def test_a_heading_with_a_space_in_it_stays_one_heading():
    """"Withdrawal Amt." is one column; "Value Dt" and "Withdrawal" are two."""
    labels = [text for text, _, _ in group_labels(HDFC_HEADER)]

    assert labels == [
        "Date", "Narration", "Chq./Ref.No.", "Value Dt",
        "Withdrawal Amt.", "Deposit Amt.", "Closing Balance",
    ]


def test_the_threshold_is_measured_from_the_line_not_assumed():
    """Word spaces and column gaps are two populations, and the line shows both.

    A fixed threshold has to sit between 1.8pt and 12.7pt on this statement,
    and between quite different numbers on one set two points smaller. Measured
    per line, both work.
    """
    threshold = label_threshold(HDFC_HEADER)

    assert 1.8 < threshold < 12.7
    assert threshold <= LABEL_GAP  # never looser than the fixed ceiling


def test_a_single_gap_falls_back_to_the_ceiling():
    """Two words give one gap, which cannot tell a space from a boundary."""
    assert label_threshold([word("Value", 100.0, 120.0), word("Dt", 130.0, 140.0)]) == LABEL_GAP


# --- columns ----------------------------------------------------------------


def test_a_header_line_becomes_named_columns():
    columns = read_header(HDFC_HEADER)

    assert columns is not None
    assert column_map(columns) == {
        "date": 0, "narration": 1, "value_date": 3,
        "debit": 4, "credit": 5, "balance": 6,
    }


def test_a_line_that_names_no_date_is_not_a_header():
    assert read_header([word("Narration", 40), word("Debit", 200), word("Credit", 300)]) is None


def test_a_header_with_no_money_column_is_refused():
    """A date and some prose is a letter, not a ledger."""
    assert read_header([word("Date", 40), word("Notes", 200), word("Signed", 300)]) is None


def test_prose_is_not_mistaken_for_a_header():
    line = [word(w, 40 + i * 50) for i, w in enumerate(["Dear", "customer", "thank", "you"])]

    assert read_header(line) is None


def test_a_right_aligned_amount_lands_under_its_own_heading():
    """The point of the whole module.

    Amounts are right-aligned and their headings are not, so neither edge of a
    heading is where its column ends. The boundary is the middle of the empty
    space between headings, and these are the real positions from an HDFC
    statement: 100.00 is a withdrawal and 2,000.00 is a deposit, and nothing but
    x position says so.
    """
    columns = read_header(HDFC_HEADER)
    names = [c.name for c in columns]

    withdrawal = word("100.00", 365.0, 390.0)
    deposit = word("2,000.00", 420.0, 450.0)

    assert names[place(withdrawal, columns)] == "debit"
    assert names[place(deposit, columns)] == "credit"


def test_a_line_is_split_into_the_columns_it_was_printed_under():
    columns = read_header(HDFC_HEADER)
    line = [
        word("04/01/16", 40), word("POS", 80), word("532676XXXXXX3201", 96),
        word("000000590060A1", 200), word("06/01/16", 290),
        word("100.00", 365.0, 390.0), word("398.82", 490.0, 512.0),
    ]

    cells = line_to_cells(line, columns)

    assert cells[column_map(columns)["date"]] == "04/01/16"
    assert cells[column_map(columns)["debit"]] == "100.00"
    assert cells[column_map(columns)["credit"]] == ""
    assert cells[column_map(columns)["balance"]] == "398.82"


# --- rows -------------------------------------------------------------------


def dated(top: float, date: str, narration: str, debit: str = "", balance: str = ""):
    line = [word(date, 40, top=top), word(narration, 80, top=top)]
    if debit:
        line.append(word(debit, 365.0, 390.0, top=top))
    if balance:
        line.append(word(balance, 490.0, 512.0, top=top))
    return line


def test_a_row_starts_at_a_date_and_absorbs_what_follows():
    columns = read_header(HDFC_HEADER)
    lines = [
        dated(200, "04/01/16", "POS", "100.00", "398.82"),
        [word("CONTINUED", 80, top=211)],
        dated(222, "11/01/16", "NEFT", "1600.00", "1387.37"),
    ]

    rows = assemble(lines, columns)

    assert len(rows) == 2
    assert "CONTINUED" in rows[0].cells[column_map(columns)["narration"]]


def test_amounts_on_the_line_below_the_date_are_found():
    """The real HDFC wrap: date on the first line, money on the second.

    Losing this drops the transaction's amount entirely, which the totals check
    would catch -- but only after the export had already been written.
    """
    columns = read_header(HDFC_HEADER)
    lines = [
        [word("04/01/16", 40, top=200), word("SALARY-123", 80, top=200)],
        [
            word("-PRIVATE", 80, top=211),
            word("18,396.00", 416.0, 450.0, top=211),
            word("19,783.37", 481.0, 512.0, top=211),
        ],
    ]

    rows = assemble(lines, columns)

    assert len(rows) == 1
    cells = rows[0].cells
    assert cells[column_map(columns)["credit"]] == "18,396.00"
    assert cells[column_map(columns)["balance"]] == "19,783.37"


def test_text_above_the_first_transaction_is_not_made_into_a_row():
    """Held, not invented: a phantom row would reconcile against a wrong total."""
    columns = read_header(HDFC_HEADER)
    lines = [
        [word("Amazon", 80, top=190), word("Pay", 120, top=190)],
        dated(200, "04/01/16", "POS", "100.00", "398.82"),
    ]

    rows = assemble(lines, columns)

    assert len(rows) == 1
    assert "Amazon" in rows[0].cells[column_map(columns)["narration"]]


def test_a_closing_marker_ends_the_absorbing():
    """Footer prose must not be swallowed into the last transaction.

    "Closing balance includes funds earmarked for hold" sits under the table on
    a real HDFC statement. Absorbed into the narration above it, the endpoint
    it announces is never seen by the code that looks for endpoints.
    """
    columns = read_header(HDFC_HEADER)
    lines = [
        dated(200, "04/01/16", "POS", "100.00", "398.82"),
        [word("CLOSING", 80, top=211), word("BALANCE", 130, top=211),
         word("398.82", 490.0, 512.0, top=211)],
        [word("This", 80, top=222), word("is", 110, top=222),
         word("computer", 130, top=222), word("generated", 190, top=222)],
    ]

    rows = assemble(lines, columns)
    narration = column_map(columns)["narration"]

    assert "CLOSING" not in rows[0].cells[narration]
    assert "computer" not in rows[0].cells[narration]
    assert any("CLOSING" in cell for cell in rows[1].cells)
