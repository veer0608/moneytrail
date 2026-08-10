"""Rows in, Statement out.

A CSV export and a PDF table differ only in how the grid is recovered. Once you
have rows, everything that turns them into a reconcilable statement -- column
aliasing, balance-marker rows, wrapped narrations, the debit/credit vs signed
layouts -- is identical. It lives here so a new format is a grid extractor and
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..models import BalanceSource, Direction, Statement, Transaction
from ..money import Paise, is_placeholder, parse_optional_amount
from .base import (
    UnparseableStatement,
    is_closing_row,
    is_opening_row,
    is_summary_heading,
    looks_like_date,
    normalise_header,
    parse_date,
)

DATE_ALIASES = ("date", "txn date", "transaction date", "tran date", "posting date")
VALUE_DATE_ALIASES = ("value date", "value dt")
NARRATION_ALIASES = (
    "narration",
    "description",
    "particulars",
    "transaction remarks",
    "remarks",
    "details",
    "transaction details",
)
DEBIT_ALIASES = (
    "withdrawal amt",
    "withdrawal amount",
    "withdrawal",
    "withdrawal (dr)",
    "debit",
    "debit amount",
    "dr",
    "paid out",
)
CREDIT_ALIASES = (
    "deposit amt",
    "deposit amount",
    "deposit",
    "deposit (cr)",
    "credit",
    "credit amount",
    "cr",
    "paid in",
)
BALANCE_ALIASES = (
    "closing balance",
    "balance",
    "running balance",
    "balance (inr)",
    "available balance",
)
AMOUNT_ALIASES = ("amount", "transaction amount", "amount (inr)")

#: value_date is probed before date so "Value Dt" is not claimed by "date".
_COLUMN_GROUPS = (
    ("value_date", VALUE_DATE_ALIASES),
    ("date", DATE_ALIASES),
    ("narration", NARRATION_ALIASES),
    ("debit", DEBIT_ALIASES),
    ("credit", CREDIT_ALIASES),
    ("balance", BALANCE_ALIASES),
    ("amount", AMOUNT_ALIASES),
)

KNOWN_BANKS = (
    "hdfc",
    "icici",
    "sbi",
    "state bank of india",
    "axis",
    "kotak",
    "yes bank",
    "idfc",
    "indusind",
    "pnb",
    "canara",
    "bank of baroda",
    "au small finance",
    "federal bank",
)


@dataclass(frozen=True)
class RawRow:
    """One extracted row, carrying enough to point a human back at the source."""

    number: int
    cells: list[str] = field(default_factory=list)
    page: int | None = None

    def cell(self, index: int | None) -> str:
        if index is None or index >= len(self.cells):
            return ""
        return self.cells[index].strip()

    @property
    def blank(self) -> bool:
        return not any(cell.strip() for cell in self.cells)

    @property
    def divider(self) -> bool:
        """A section separator drawn out of asterisks, dashes or equals signs."""
        filled = [cell for cell in self.cells if cell.strip()]
        return bool(filled) and all(is_placeholder(cell) for cell in filled)


def clean_cell(value: str | None) -> str:
    """PDF table cells arrive as None or with newlines baked in."""
    if value is None:
        return ""
    return " ".join(value.split())


def map_columns(cells: Sequence[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, cell in enumerate(cells):
        header = normalise_header(cell)
        if not header:
            continue
        for field_name, aliases in _COLUMN_GROUPS:
            if field_name in columns:
                continue
            if header in aliases:
                columns[field_name] = index
                break
    return columns


def is_header(cells: Sequence[str]) -> dict[str, int] | None:
    """Return the column map if these cells look like a statement header."""
    columns = map_columns(cells)
    has_date = "date" in columns
    has_money = (
        "balance" in columns
        or "amount" in columns
        or ("debit" in columns and "credit" in columns)
    )
    return columns if (has_date and has_money) else None


def find_header(
    rows: Sequence[Sequence[str]], limit: int = 40
) -> tuple[int, dict[str, int]] | None:
    """Locate the header row. Bank exports put account details above the table."""
    for index, cells in enumerate(rows[:limit]):
        columns = is_header(cells)
        if columns is not None:
            return index, columns
    return None


def detect_bank(preamble: Sequence[str], path: Path) -> str:
    haystack = " ".join(preamble).lower()
    for source in (haystack, path.stem.lower()):
        for bank in KNOWN_BANKS:
            if bank in source:
                return bank.upper() if len(bank) <= 5 else bank.title()
    return "unknown"


def build_statement(
    *,
    source: Path,
    bank: str,
    account_hint: str,
    columns: dict[str, int],
    rows: Sequence[RawRow],
) -> Statement:
    transactions: list[Transaction] = []
    opening: Paise | None = None
    opening_source = BalanceSource.EXPLICIT
    closing: Paise | None = None
    closing_source = BalanceSource.EXPLICIT

    for position, row in enumerate(rows):
        # Blank rows and ruled dividers ("*****", "-----") carry no ledger
        # information. Real exports are full of both.
        if row.blank or row.divider:
            continue

        # A summary block ends the ledger. Read its stated endpoints first --
        # they are the bank's own totals, which turns the reconciliation into a
        # comparison against the bank rather than a self-consistency check.
        if transactions and _starts_summary(row):
            stated_opening, stated_closing = _read_summary_block(rows, position)
            if stated_opening is not None and opening is None:
                opening = stated_opening
            if stated_closing is not None:
                closing = stated_closing
            break

        # Endpoint markers are checked before amounts. In a summary block the
        # label and its value sit in different columns, and the other cells of
        # that row are often prose that will not parse as money.
        marker = _balance_marker(row)
        if marker is not None:
            value = _any_amount(row, columns)
            if marker == "opening":
                if value is not None and opening is None:
                    opening = value
                continue
            if value is not None:
                closing = value
            if transactions:
                # Nothing after the closing balance is a transaction. Stop even
                # when the value is not on this row -- summary blocks often put
                # labels and figures on separate lines -- and let the closing
                # balance fall back to the last row, flagged as derived.
                break
            continue

        narration = row.cell(columns.get("narration"))
        try:
            balance = parse_optional_amount(row.cell(columns.get("balance")))
            debit, credit = _amounts(row, columns)
        except ValueError as exc:
            # A number we cannot read is a located parser failure, not a crash.
            raise UnparseableStatement(f"{source}: {_where(row)}: {exc}") from exc

        if debit is None and credit is None:
            if balance is None and not looks_like_date(row.cell(columns.get("date"))):
                # A wrapped narration line belonging to the row above.
                if transactions:
                    previous = transactions[-1]
                    transactions[-1] = _with_narration(
                        previous, f"{previous.narration} {narration}".strip()
                    )
                    continue
            raise UnparseableStatement(
                f"{source}: {_where(row)} has no debit or credit and is not a "
                f"balance marker: {row.cells!r}"
            )

        if debit is not None and credit is not None:
            raise UnparseableStatement(
                f"{source}: {_where(row)} is both a debit and a credit: {row.cells!r}"
            )

        amount = debit if debit is not None else credit
        assert amount is not None  # narrows for type checkers
        transactions.append(
            Transaction(
                row=row.number,
                page=row.page,
                date=parse_date(row.cell(columns.get("date"))),
                narration=narration,
                direction=Direction.DEBIT if debit is not None else Direction.CREDIT,
                amount=abs(amount),
                balance=balance,
                value_date=_optional_date(row.cell(columns.get("value_date"))),
            )
        )

    if not transactions:
        raise UnparseableStatement(f"{source}: header found but no transaction rows")

    if opening is None:
        first = transactions[0]
        if first.balance is None:
            raise UnparseableStatement(
                f"{source}: no opening balance stated and no running balance column "
                f"to derive one from"
            )
        opening = first.balance - first.signed
        opening_source = BalanceSource.DERIVED

    if closing is None:
        last = transactions[-1]
        if last.balance is None:
            raise UnparseableStatement(
                f"{source}: no closing balance stated and the last row carries no "
                f"running balance"
            )
        closing = last.balance
        closing_source = BalanceSource.DERIVED

    return Statement(
        source=source,
        bank=bank,
        account_hint=account_hint,
        opening_balance=opening,
        closing_balance=closing,
        transactions=tuple(transactions),
        period_start=transactions[0].date,
        period_end=transactions[-1].date,
        opening_source=opening_source,
        closing_source=closing_source,
    )


def _amounts(row: RawRow, columns: dict[str, int]) -> tuple[Paise | None, Paise | None]:
    """Return ``(debit, credit)`` for either column layout."""
    if "debit" in columns or "credit" in columns:
        return (
            parse_optional_amount(row.cell(columns.get("debit"))),
            parse_optional_amount(row.cell(columns.get("credit"))),
        )
    signed = parse_optional_amount(row.cell(columns.get("amount")))
    if signed is None:
        return None, None
    return (None, signed) if signed >= 0 else (-signed, None)


def _starts_summary(row: RawRow) -> bool:
    """A summary heading, or a label row naming both endpoints at once."""
    if any(is_summary_heading(cell) for cell in row.cells):
        return True
    return any(is_opening_row(cell) for cell in row.cells) and any(
        is_closing_row(cell) for cell in row.cells
    )


def _read_summary_block(
    rows: Sequence[RawRow], position: int, lookahead: int = 6
) -> tuple[Paise | None, Paise | None]:
    """Read ``(opening, closing)`` out of a labels-over-values summary table."""
    labels = next(
        (
            row
            for row in rows[position : position + lookahead]
            if any(is_opening_row(cell) for cell in row.cells)
            or any(is_closing_row(cell) for cell in row.cells)
        ),
        None,
    )
    if labels is None:
        return None, None

    opening_at = _column_of(labels, is_opening_row)
    closing_at = _column_of(labels, is_closing_row)

    opening = closing = None
    start = rows.index(labels) + 1
    for row in rows[start : start + lookahead]:
        if opening is None and opening_at is not None:
            opening = _cell_amount(row, opening_at)
        if closing is None and closing_at is not None:
            closing = _cell_amount(row, closing_at)
        if opening is not None and closing is not None:
            break
    return opening, closing


def _column_of(row: RawRow, predicate) -> int | None:
    return next(
        (index for index, cell in enumerate(row.cells) if predicate(cell)), None
    )


def _cell_amount(row: RawRow, index: int) -> Paise | None:
    try:
        return parse_optional_amount(row.cell(index))
    except ValueError:
        return None


def _balance_marker(row: RawRow) -> str | None:
    """"opening" / "closing" if any cell of the row labels an endpoint."""
    for cell in row.cells:
        if is_opening_row(cell):
            return "opening"
        if is_closing_row(cell):
            return "closing"
    return None


def _any_amount(row: RawRow, columns: dict[str, int]) -> Paise | None:
    """The endpoint value on a marker row: balance column first, then any cell."""
    ordered = [row.cell(columns.get("balance"))] + list(row.cells)
    for cell in ordered:
        try:
            value = parse_optional_amount(cell)
        except ValueError:
            continue  # prose in a summary row, keep looking
        if value is not None:
            return value
    return None


def _optional_date(text: str):
    return parse_date(text) if text and looks_like_date(text) else None


def _where(row: RawRow) -> str:
    if row.page is None:
        return f"row {row.number}"
    return f"row {row.number} of page {row.page}"


def _with_narration(txn: Transaction, narration: str) -> Transaction:
    return Transaction(
        row=txn.row,
        page=txn.page,
        date=txn.date,
        narration=narration,
        direction=txn.direction,
        amount=txn.amount,
        balance=txn.balance,
        value_date=txn.value_date,
    )
