"""Decide what kind of statement a recovered grid is, and build it.

Format parsers recover a grid and stop there. Whether those rows describe a
bank account or a credit card is a property of the content, not the file type,
so the decision lives in one place rather than three.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..models import CardStatement, Statement
from .base import find_account_hint
from .card import build_card_statement, detect_issuer, looks_like_card
from .table import RawRow, build_statement, detect_bank


def build(
    *,
    source: Path,
    columns: dict[str, int],
    rows: Sequence[RawRow],
    grid: Sequence[RawRow],
    preamble: Sequence[str],
) -> Statement | CardStatement:
    account_hint = find_account_hint(preamble)

    # A running-balance column settles it. Bank statements have one and card
    # statements do not, which stops a narration like "CREDIT CARD PAYMENT" on a
    # bank statement from dragging the whole file onto the card path.
    if "balance" not in columns and looks_like_card([row.cells for row in grid]):
        return build_card_statement(
            source=source,
            issuer=detect_issuer(preamble, source),
            account_hint=account_hint,
            columns=columns,
            rows=rows,
            grid=grid,
        )

    return build_statement(
        source=source,
        bank=detect_bank(preamble, source),
        account_hint=account_hint,
        columns=columns,
        rows=rows,
    )
