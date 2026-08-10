"""The domain: a statement is an opening balance, a list of rows, and a close."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

from .money import Paise


class Direction(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class BalanceSource(str, Enum):
    #: Taken from the statement itself (a header field or a brought-forward row).
    EXPLICIT = "explicit"
    #: Reconstructed from the first/last row's running balance because the
    #: statement did not state it. Still checkable, but weaker: see reconcile().
    DERIVED = "derived"


@dataclass(frozen=True)
class Transaction:
    """One row. ``amount`` is a magnitude; ``direction`` carries the sign."""

    row: int
    date: date
    narration: str
    direction: Direction
    amount: Paise
    balance: Paise | None = None
    value_date: date | None = None
    #: Set for formats where a row number alone is not enough to find the row
    #: again -- a six-page PDF, for instance.
    page: int | None = None

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(
                f"row {self.row}: amount must be a magnitude, got {self.amount}"
            )

    @property
    def signed(self) -> Paise:
        return self.amount if self.direction is Direction.CREDIT else -self.amount


@dataclass(frozen=True)
class Statement:
    source: Path
    bank: str
    account_hint: str
    opening_balance: Paise
    closing_balance: Paise
    transactions: tuple[Transaction, ...]
    period_start: date | None = None
    period_end: date | None = None
    opening_source: BalanceSource = BalanceSource.EXPLICIT
    closing_source: BalanceSource = BalanceSource.EXPLICIT

    @property
    def rows_with_balance(self) -> int:
        return sum(1 for txn in self.transactions if txn.balance is not None)


@dataclass(frozen=True)
class CardSummary:
    """The box a credit-card statement prints its own totals in.

    Every field is optional because issuers print different subsets, and a
    missing figure must mean "not checkable" rather than zero.
    """

    previous_balance: Paise | None = None
    payments: Paise | None = None
    purchases: Paise | None = None
    fees: Paise | None = None
    total_due: Paise | None = None
    minimum_due: Paise | None = None
    due_date: date | None = None

    @property
    def stated_debits(self) -> Paise | None:
        """Purchases plus finance charges: what the rows should add up to."""
        if self.purchases is None:
            return None
        return self.purchases + (self.fees or 0)


@dataclass(frozen=True)
class CardStatement:
    """A credit-card statement.

    Transactions reuse :class:`Transaction` (with no running balance) so
    everything built on a ledger -- merchant rollups especially -- works here
    unchanged. That matters: card purchases are the merchant-heavy data a bank
    statement does not contain.
    """

    source: Path
    issuer: str
    account_hint: str
    summary: CardSummary
    transactions: tuple[Transaction, ...]
    period_start: date | None = None
    period_end: date | None = None

    @property
    def rows_with_balance(self) -> int:
        return 0
