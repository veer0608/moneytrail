"""Credit-card statements.

Structurally a different animal from a bank statement: no running balance, so
there is no chain to walk. What a card statement does publish is a summary box
-- previous balance, payments, purchases, total due -- and that box is the
ground truth. Two things get checked against it: the box's own arithmetic, and
whether the transaction rows this parse recovered add up to the totals the box
claims.

The sign convention is the trap. On a card, ``Cr`` means money coming off what
you owe (a payment or a refund) and a bare amount is a purchase -- the opposite
reading from a bank statement's credit column.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from ..models import CardStatement, CardSummary, Direction, Transaction
from ..money import Paise, parse_optional_amount
from .base import (
    UnparseableStatement,
    infer_date_order,
    looks_like_date,
    parse_date,
)
from .table import RawRow, normalise_header

#: Labels no bank statement carries. Deliberately excludes "closing balance",
#: which would drag every bank statement into the card path.
CARD_MARKERS = re.compile(
    r"min(imum)?\s+(amount\s+)?due"
    r"|total\s+(amount\s+)?dues?\b"
    r"|payment\s+due\s+date"
    r"|credit\s+card",
    re.IGNORECASE,
)

_SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("minimum_due", r"min(imum)?\s+(amount\s+)?dues?"),
    ("total_due", r"total\s+(amount\s+)?dues?|total\s+payable|net\s+amount\s+due"),
    ("previous_balance", r"(previous|opening|last)\s+(statement\s+)?bal(ance)?|past\s+dues?"),
    ("payments", r"payments?\s*[/&]\s*credits?|payments?\b|credits?\s*[/&]\s*payments?"),
    ("purchases", r"purchases?\s*[/&]\s*debits?|purchases?\b|new\s+debits?|total\s+spends?"),
    ("fees", r"finance\s+charges?|interest\s+charges?|other\s+charges?|fees?\s*[&/]?\s*charges?"),
)
_DUE_DATE = re.compile(r"payment\s+due\s+date|due\s+date", re.IGNORECASE)

_CARD_ISSUERS = (
    "hdfc", "icici", "sbi", "axis", "kotak", "amex", "american express",
    "citi", "standard chartered", "rbl", "indusind", "au bank", "yes bank",
    "onecard", "slice", "idfc",
)


def looks_like_card(grid: Sequence[Sequence[str]]) -> bool:
    return any(CARD_MARKERS.search(cell) for row in grid for cell in row)


def detect_issuer(preamble: Sequence[str], path: Path) -> str:
    haystack = " ".join(preamble).lower()
    for source in (haystack, path.stem.lower()):
        for issuer in _CARD_ISSUERS:
            if issuer in source:
                return issuer.upper() if len(issuer) <= 5 else issuer.title()
    return "unknown"


def build_card_statement(
    *,
    source: Path,
    issuer: str,
    account_hint: str,
    columns: dict[str, int],
    rows: Sequence[RawRow],
    grid: Sequence[RawRow],
) -> CardStatement:
    """``rows`` are the transaction rows; ``grid`` is every row, for the summary."""
    # Same reading the bank path settles, for the same reason: nothing in a
    # card's arithmetic depends on a date, so the wrong order passes silently.
    date_at = columns.get("date")
    date_order, _ = infer_date_order(
        [row.cell(date_at) for row in rows] if date_at is not None else []
    )

    transactions: list[Transaction] = []

    for row in rows:
        if row.blank or row.divider:
            continue
        date_text = row.cell(columns.get("date"))
        if not looks_like_date(date_text):
            # Summary lines and footers live in the same table on many cards.
            continue
        try:
            direction, amount = _direction_and_amount(row, columns)
        except ValueError as exc:
            raise UnparseableStatement(
                f"{source}: row {row.number}: {exc}"
            ) from exc
        if amount is None:
            continue
        transactions.append(
            Transaction(
                row=row.number,
                page=row.page,
                date=parse_date(date_text, date_order),
                narration=row.cell(columns.get("narration")),
                direction=direction,
                amount=abs(amount),
            )
        )

    if not transactions:
        raise UnparseableStatement(f"{source}: header found but no transaction rows")

    return CardStatement(
        source=source,
        issuer=issuer,
        account_hint=account_hint,
        summary=read_summary(grid),
        transactions=tuple(transactions),
        period_start=min(txn.date for txn in transactions),
        period_end=max(txn.date for txn in transactions),
    )


def read_summary(grid: Sequence[RawRow]) -> CardSummary:
    values: dict[str, Paise] = {}
    for field, pattern in _SUMMARY_FIELDS:
        found = _labelled_amount(grid, re.compile(pattern, re.IGNORECASE))
        if found is not None:
            values[field] = found
    return CardSummary(due_date=_labelled_date(grid), **values)


def _labelled_amount(grid: Sequence[RawRow], label: re.Pattern) -> Paise | None:
    """Find ``label`` in any cell, then the figure that belongs to it.

    Issuers put the value beside the label or directly beneath it, so both are
    tried -- beside first, since that is far commoner.
    """
    for index, row in enumerate(grid):
        for column, cell in enumerate(row.cells):
            if not label.match(normalise_header(cell)):
                continue
            beside = _first_amount(row.cells[column + 1 :])
            if beside is not None:
                return beside
            for below in grid[index + 1 : index + 3]:
                value = _amount_or_none(below.cell(column))
                if value is not None:
                    return value
    return None


def _labelled_date(grid: Sequence[RawRow]):
    for index, row in enumerate(grid):
        for column, cell in enumerate(row.cells):
            if not _DUE_DATE.match(normalise_header(cell)):
                continue
            candidates = list(row.cells[column + 1 :]) + [
                below.cell(column) for below in grid[index + 1 : index + 3]
            ]
            for candidate in candidates:
                if candidate and looks_like_date(candidate):
                    return parse_date(candidate)
    return None


def _first_amount(cells: Sequence[str]) -> Paise | None:
    for cell in cells:
        value = _amount_or_none(cell)
        if value is not None:
            return value
    return None


def _amount_or_none(cell: str) -> Paise | None:
    try:
        return parse_optional_amount(cell)
    except ValueError:
        return None


def _direction_and_amount(
    row: RawRow, columns: dict[str, int]
) -> tuple[Direction, Paise | None]:
    if "debit" in columns or "credit" in columns:
        debit = parse_optional_amount(row.cell(columns.get("debit")))
        credit = parse_optional_amount(row.cell(columns.get("credit")))
        if debit is not None:
            return Direction.DEBIT, debit
        return Direction.CREDIT, credit

    raw = row.cell(columns.get("amount"))
    value = parse_optional_amount(raw)
    if value is None:
        return Direction.DEBIT, None
    # "Cr" marks money coming off the balance; so does a negative, and so does
    # the leading "+" HDFC puts on a payment -- on a card the sign is about the
    # balance owed, not the account, so a plus is money coming off what you owe.
    # Everything else is a purchase.
    cleaned = raw.strip().lower()
    credited = cleaned.endswith("cr") or value < 0 or cleaned.startswith("+")
    return (Direction.CREDIT if credited else Direction.DEBIT), value
