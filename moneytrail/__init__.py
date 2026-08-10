"""moneytrail -- turn bank statements into a ledger that provably adds up."""

from __future__ import annotations

from .models import (
    BalanceSource,
    CardStatement,
    CardSummary,
    Direction,
    Statement,
    Transaction,
)
from .money import Paise, format_paise, parse_amount, parse_optional_amount
from .parsers import parse_statement
from .reconcile import (
    CardReconciliation,
    Discrepancy,
    Reconciliation,
    is_tautological,
    reconcile,
    reconcile_card,
)

__version__ = "0.1.0"

__all__ = [
    "BalanceSource",
    "CardReconciliation",
    "CardStatement",
    "CardSummary",
    "Direction",
    "Discrepancy",
    "Paise",
    "Reconciliation",
    "Statement",
    "Transaction",
    "format_paise",
    "is_tautological",
    "parse_amount",
    "parse_optional_amount",
    "parse_statement",
    "reconcile",
    "reconcile_card",
]
