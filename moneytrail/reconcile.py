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

from .models import BalanceSource, CardStatement, Direction, Statement
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

    # A third check, where the bank prints its own column totals -- Axis labels
    # that row TRANSACTION TOTAL.
    #
    # It closes the one hole the other two cannot. When both endpoints are
    # derived from the rows, a row lost off the *end* takes the closing balance
    # with it: the chain stays consistent, opening + credits - debits still
    # equals the new close, and both checks report a clean statement that is
    # missing a transaction. A total the bank published separately does not
    # move when a row goes missing, so it is the only figure that notices.
    for kind, computed, stated in (
        ("stated-debits", debits, statement.stated_debits),
        ("stated-credits", credits, statement.stated_credits),
    ):
        if stated is not None and computed != stated:
            discrepancies.append(
                Discrepancy(
                    kind=kind,
                    expected=computed,
                    actual=stated,
                    narration=f"summed from the rows against the statement's own {kind[7:]} total",
                )
            )

    return Reconciliation(
        statement=statement,
        credits=credits,
        debits=debits,
        computed_closing=computed_closing,
        discrepancies=tuple(discrepancies),
    )


@dataclass(frozen=True)
class CardReconciliation:
    """A card statement has no running balance, so the checks are different.

    ``summary`` is the statement's own arithmetic; ``rows`` is what this parse
    recovered. Both are needed: the first catches a misread summary box, the
    second catches transaction rows that were dropped or misread -- and only
    the second says anything about whether the ledger is complete.
    """

    statement: CardStatement
    row_debits: Paise
    row_credits: Paise
    discrepancies: tuple[Discrepancy, ...]
    checks: tuple[str, ...]
    #: Sub-rupee gaps in the issuer's *own* summary box. Reported, not failed:
    #: see ROUNDING_TOLERANCE.
    roundings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    @property
    def verified(self) -> bool:
        """Whether the parse was checked against the rows at all."""
        return "rows-debit" in self.checks or "rows-credit" in self.checks

    def report(self) -> str:
        stmt = self.statement
        summary = stmt.summary
        lines = [
            str(stmt.source),
            f"  card            {stmt.issuer}",
            f"  account         {stmt.account_hint or '-'}",
            f"  period          {stmt.period_start or '?'} -> {stmt.period_end or '?'}",
            f"  previous      {_show(summary.previous_balance):>16}",
            f"  payments    - {_show(summary.payments):>16}",
            f"  purchases   + {_show(summary.purchases):>16}",
            f"  fees        + {_show(summary.fees):>16}",
            f"  total due     {_show(summary.total_due):>16}",
        ]
        if summary.minimum_due is not None:
            lines.append(f"  minimum due   {_show(summary.minimum_due):>16}")
        if summary.due_date is not None:
            lines.append(f"  due by          {summary.due_date}")
        # Stated against recovered, side by side, so a gap is obvious. The debit
        # side is compared to purchases + fees, not purchases alone.
        lines.append(
            f"  rows            {len(stmt.transactions)} transactions -- "
            f"{format_paise(self.row_debits)} charged "
            f"(stated {_show(summary.stated_debits)}), "
            f"{format_paise(self.row_credits)} paid off "
            f"(stated {_show(summary.payments)})"
        )

        if not self.checks:
            lines.append(
                "  UNVERIFIED -- the statement prints no totals to check the rows against"
            )
        elif self.ok:
            ran = ", ".join(self.checks)
            note = "" if self.verified else " (summary only -- rows not cross-checked)"
            lines.append(f"  RECONCILED to the paisa [{ran}]{note}")
            lines += [f"    note: {r}" for r in self.roundings]
        else:
            lines.append(f"  FAILED -- {len(self.discrepancies)} discrepancy(ies):")
            lines += [f"    [{d.kind}] {d.describe()}" for d in self.discrepancies]
        return "\n".join(lines)


#: Issuers round the total they ask you to pay. HDFC prints previous minus
#: payments plus purchases as ₹19,375.19 and then bills ₹19,375.00 -- the
#: rounding is theirs, and the parse that recovered both figures is exact.
#:
#: Tolerated on the summary check only, because that one compares the issuer's
#: published figures to each other and says nothing about whether the ledger is
#: complete. The row checks stay exact to the paisa: those are the completeness
#: proof, and a tolerance there would let a missing transaction hide inside it.
#:
#: Failing a whole statement over nineteen paise of the bank's own rounding
#: would put a red verdict on every HDFC card statement, and a red verdict
#: everyone learns to dismiss is worse than none.
ROUNDING_TOLERANCE = 100  # one rupee, in paise


def reconcile_card(statement: CardStatement) -> CardReconciliation:
    summary = statement.summary
    row_debits = sum(
        t.amount for t in statement.transactions if t.direction is Direction.DEBIT
    )
    row_credits = sum(
        t.amount for t in statement.transactions if t.direction is Direction.CREDIT
    )

    discrepancies: list[Discrepancy] = []
    roundings: list[str] = []
    checks: list[str] = []

    if None not in (summary.previous_balance, summary.payments, summary.purchases, summary.total_due):
        checks.append("summary")
        computed = (
            summary.previous_balance
            - summary.payments
            + summary.purchases
            + (summary.fees or 0)
        )
        if computed != summary.total_due:
            gap = Discrepancy(
                kind="summary",
                expected=computed,
                actual=summary.total_due,
                narration="previous - payments + purchases + fees",
            )
            if abs(gap.delta) < ROUNDING_TOLERANCE:
                roundings.append(
                    f"the issuer's own summary is out by {_show(gap.delta)}, "
                    f"which is it rounding the total it billed rather than "
                    f"anything about this parse"
                )
            else:
                discrepancies.append(gap)

    stated_debits = summary.stated_debits
    if stated_debits is not None:
        checks.append("rows-debit")
        if row_debits != stated_debits:
            discrepancies.append(
                Discrepancy(
                    kind="rows-debit",
                    expected=stated_debits,
                    actual=row_debits,
                    narration="purchases and fees, summed from the rows",
                )
            )

    if summary.payments is not None:
        checks.append("rows-credit")
        if row_credits != summary.payments:
            discrepancies.append(
                Discrepancy(
                    kind="rows-credit",
                    expected=summary.payments,
                    actual=row_credits,
                    narration="payments and refunds, summed from the rows",
                )
            )

    return CardReconciliation(
        statement=statement,
        row_debits=row_debits,
        row_credits=row_credits,
        discrepancies=tuple(discrepancies),
        checks=tuple(checks),
        roundings=tuple(roundings),
    )


def _show(paise: Paise | None) -> str:
    return format_paise(paise) if paise is not None else "-"


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
