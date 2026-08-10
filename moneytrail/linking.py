"""Match bank-side card repayments to the card-side payments they settle.

A card bill payment is one event recorded twice: a debit leaving the bank
account, and a credit reducing what the card says you owe. Add both statements
together naively and the money is counted twice -- once as the purchases on the
card, once as the repayment on the bank statement.

Linking them fixes that, but only where a card statement actually exists. A
repayment with no matching card statement stays counted, because the purchases
it settled are not in front of us; dropping it would understate spending and
quietly flatter the total. Understating is the more dangerous error, so the
conservative side is the default and the report says which repayments got which
treatment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .merchants import Kind, build_vocabulary, identify
from .models import CardStatement, Direction, Statement, Transaction
from .money import Paise

#: How long a payment may take to appear on the card, in days, relative to the
#: bank debit. A little negative slack covers issuers that post on value date.
DEFAULT_WINDOW = (-2, 7)


@dataclass(frozen=True)
class RepaymentLink:
    bank_source: Path
    bank_transaction: Transaction
    card_source: Path | None = None
    card_transaction: Transaction | None = None

    @property
    def matched(self) -> bool:
        return self.card_transaction is not None

    @property
    def amount(self) -> Paise:
        return self.bank_transaction.amount

    @property
    def lag_days(self) -> int | None:
        if self.card_transaction is None:
            return None
        return (self.card_transaction.date - self.bank_transaction.date).days


@dataclass(frozen=True)
class Linkage:
    links: tuple[RepaymentLink, ...]
    #: Payments a card acknowledges that no supplied bank statement explains --
    #: usually settled from an account that was not provided.
    orphan_card_payments: tuple[tuple[Path, Transaction], ...]

    @property
    def matched(self) -> tuple[RepaymentLink, ...]:
        return tuple(link for link in self.links if link.matched)

    @property
    def unmatched(self) -> tuple[RepaymentLink, ...]:
        return tuple(link for link in self.links if not link.matched)

    @property
    def matched_total(self) -> Paise:
        return sum(link.amount for link in self.matched)

    @property
    def unmatched_total(self) -> Paise:
        return sum(link.amount for link in self.unmatched)


@dataclass(frozen=True)
class SpendSummary:
    bank_outflow: Paise
    matched_repayments: Paise
    unmatched_repayments: Paise
    card_charges: Paise

    @property
    def true_outflow(self) -> Paise:
        """What actually left your control.

        Card purchases counted where they happened, and the repayments that
        settle them removed. Repayments with no card statement behind them stay
        in, standing in for purchases we cannot see.
        """
        return self.bank_outflow - self.matched_repayments + self.card_charges

    @property
    def complete(self) -> bool:
        return self.unmatched_repayments == 0


def card_repayments(statement: Statement) -> list[Transaction]:
    """Bank debits that settle a credit card."""
    narrations = [txn.narration for txn in statement.transactions]
    vocabulary = build_vocabulary(narrations)
    return [
        txn
        for txn in statement.transactions
        if txn.direction is Direction.DEBIT
        and identify(txn.narration, vocabulary).kind is Kind.CARD
    ]


def link_card_repayments(
    bank_statements: Sequence[Statement],
    card_statements: Sequence[CardStatement],
    window: tuple[int, int] = DEFAULT_WINDOW,
) -> Linkage:
    earliest, latest = window

    # (source, transaction) for every payment a card acknowledges.
    payments: list[tuple[Path, Transaction]] = [
        (statement.source, txn)
        for statement in card_statements
        for txn in statement.transactions
        if txn.direction is Direction.CREDIT
    ]
    claimed: set[int] = set()

    links: list[RepaymentLink] = []
    for statement in bank_statements:
        for debit in sorted(card_repayments(statement), key=lambda t: (t.date, t.row)):
            best: int | None = None
            best_lag = 0
            for index, (_, payment) in enumerate(payments):
                if index in claimed or payment.amount != debit.amount:
                    continue
                lag = (payment.date - debit.date).days
                if not earliest <= lag <= latest:
                    continue
                # Nearest in time wins; the amount already matched exactly.
                if best is None or abs(lag) < abs(best_lag):
                    best, best_lag = index, lag
            if best is None:
                links.append(
                    RepaymentLink(bank_source=statement.source, bank_transaction=debit)
                )
                continue
            claimed.add(best)
            card_source, card_transaction = payments[best]
            links.append(
                RepaymentLink(
                    bank_source=statement.source,
                    bank_transaction=debit,
                    card_source=card_source,
                    card_transaction=card_transaction,
                )
            )

    orphans = tuple(
        entry for index, entry in enumerate(payments) if index not in claimed
    )
    return Linkage(links=tuple(links), orphan_card_payments=orphans)


def summarise_spend(
    bank_statements: Sequence[Statement],
    card_statements: Sequence[CardStatement],
    linkage: Linkage | None = None,
) -> SpendSummary:
    linkage = linkage or link_card_repayments(bank_statements, card_statements)
    return SpendSummary(
        bank_outflow=sum(
            txn.amount
            for statement in bank_statements
            for txn in statement.transactions
            if txn.direction is Direction.DEBIT
        ),
        matched_repayments=linkage.matched_total,
        unmatched_repayments=linkage.unmatched_total,
        card_charges=sum(
            txn.amount
            for statement in card_statements
            for txn in statement.transactions
            if txn.direction is Direction.DEBIT
        ),
    )
