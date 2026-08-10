"""The correctness gate.

Everything downstream -- categorisation, refund matching, subscription
detection -- is worthless if the ledger is not a faithful copy of the
statement. So the parse is checked against arithmetic the bank already
published, and that check is free: no labelling, no judgement, no model.

Two independent checks:

``chain``
    Walk the running-balance column. Every row must move the balance by exactly
    its own amount. This localises a fault to a row.

``totals``
    ``opening + credits - debits == closing``. This catches faults the chain
    cannot see, e.g. rows dropped off the end, or a statement with no running
    balance column at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import BalanceSource, Direction, Statement
from .money import Paise, format_paise


@dataclass(frozen=True)
class Discrepancy:
    kind: str  # "chain" | "totals"
    expected: Paise
    actual: Paise
    row: int | None = None
    narration: str = ""
    page: int | None = None

    @property
    def delta(self) -> Paise:
        return self.actual - self.expected

    def describe(self) -> str:
        if self.row is None:
            where = "statement total"
        elif self.page is None:
            where = f"row {self.row}"
        else:
            where = f"row {self.row} of page {self.page}"
        detail = (
            f"{where}: expected {format_paise(self.expected)}, "
            f"statement says {format_paise(self.actual)} "
            f"(off by {format_paise(self.delta)})"
        )
        return f"{detail} -- {self.narration}" if self.narration else detail


@dataclass(frozen=True)
class Reconciliation:
    statement: Statement
    credits: Paise
    debits: Paise
    computed_closing: Paise
    discrepancies: tuple[Discrepancy, ...]

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    @property
    def chain_checked(self) -> bool:
        return self.statement.rows_with_balance > 0

    @property
    def first_bad_row(self) -> int | None:
        rows = [d.row for d in self.discrepancies if d.row is not None]
        return min(rows) if rows else None

    def report(self) -> str:
        stmt = self.statement
        rows = len(stmt.transactions)
        lines = [
            str(stmt.source),
            f"  bank            {stmt.bank}",
            f"  account         {stmt.account_hint or '-'}",
            f"  period          {stmt.period_start or '?'} -> {stmt.period_end or '?'}",
            f"  transactions    {rows} ({stmt.rows_with_balance} carry a running balance)",
            f"  opening       {format_paise(stmt.opening_balance):>16}  ({stmt.opening_source.value})",
            f"  credits     + {format_paise(self.credits):>16}",
            f"  debits      - {format_paise(self.debits):>16}",
            f"  computed      {format_paise(self.computed_closing):>16}",
            f"  closing       {format_paise(stmt.closing_balance):>16}  ({stmt.closing_source.value})",
        ]
        if self.ok:
            note = "" if self.chain_checked else " (totals only -- no running balance column)"
            lines.append(f"  RECONCILED to the paisa{note}")
        else:
            lines.append(f"  FAILED -- {len(self.discrepancies)} discrepancy(ies):")
            lines += [f"    [{d.kind}] {d.describe()}" for d in self.discrepancies]
        return "\n".join(lines)


def reconcile(statement: Statement) -> Reconciliation:
    credits = sum(
        t.amount for t in statement.transactions if t.direction is Direction.CREDIT
    )
    debits = sum(
        t.amount for t in statement.transactions if t.direction is Direction.DEBIT
    )
    computed_closing = statement.opening_balance + credits - debits

    discrepancies: list[Discrepancy] = []

    running = statement.opening_balance
    for txn in statement.transactions:
        running += txn.signed
        if txn.balance is None:
            continue
        if txn.balance != running:
            discrepancies.append(
                Discrepancy(
                    kind="chain",
                    expected=running,
                    actual=txn.balance,
                    row=txn.row,
                    narration=txn.narration,
                    page=txn.page,
                )
            )
            # Resync to the statement's own figure. One bad row should point at
            # itself, not poison every row after it.
            running = txn.balance

    if computed_closing != statement.closing_balance:
        discrepancies.append(
            Discrepancy(
                kind="totals",
                expected=computed_closing,
                actual=statement.closing_balance,
            )
        )

    return Reconciliation(
        statement=statement,
        credits=credits,
        debits=debits,
        computed_closing=computed_closing,
        discrepancies=tuple(discrepancies),
    )


def is_tautological(statement: Statement) -> bool:
    """True when both endpoints were derived from the rows themselves.

    The totals check still catches dropped or misread rows in the middle, but it
    can no longer catch a fault in the first or last row. Worth surfacing rather
    than quietly counting it as a clean reconcile.
    """
    return (
        statement.opening_source is BalanceSource.DERIVED
        and statement.closing_source is BalanceSource.DERIVED
    )
