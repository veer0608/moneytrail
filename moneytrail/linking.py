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
#: Money moving between your own accounts lands fast; NEFT can slip a day.
TRANSFER_WINDOW = (0, 3)

#: A transaction and the statement it came from.
Entry = tuple[Path, Transaction]


@dataclass(frozen=True)
class Pairing:
    """One-to-one matches between outflows and inflows, plus what was left over.

    Card repayments and inter-account transfers are the same operation: find
    the credit on some other document that this debit produced. Both go through
    here so the matching rules cannot drift apart.
    """

    pairs: tuple[tuple[Entry, Entry], ...]
    #: Aligned one-for-one with the outflows passed in: the inflow each was
    #: matched to, or None. Positional rather than keyed by the transaction,
    #: because two outflows can be equal -- or the very same object.
    matches: tuple[Entry | None, ...]
    unmatched_out: tuple[Entry, ...]
    unmatched_in: tuple[Entry, ...]


def pair_off(
    outflows: Sequence[Entry],
    inflows: Sequence[Entry],
    window: tuple[int, int],
    *,
    across_sources_only: bool = False,
) -> Pairing:
    """Match each outflow to at most one inflow: exact amount, nearest date.

    Deterministic -- outflows are consumed in the order given, and among equally
    plausible inflows the closest in time wins.
    """
    earliest, latest = window
    claimed: set[int] = set()
    pairs: list[tuple[Entry, Entry]] = []
    matches: list[Entry | None] = []
    unmatched_out: list[Entry] = []

    for out_source, debit in outflows:
        best: int | None = None
        best_lag = 0
        for index, (in_source, credit) in enumerate(inflows):
            if index in claimed or credit.amount != debit.amount:
                continue
            if across_sources_only and in_source == out_source:
                continue
            lag = (credit.date - debit.date).days
            if not earliest <= lag <= latest:
                continue
            if best is None or abs(lag) < abs(best_lag):
                best, best_lag = index, lag
        if best is None:
            matches.append(None)
            unmatched_out.append((out_source, debit))
            continue
        claimed.add(best)
        matches.append(inflows[best])
        pairs.append(((out_source, debit), inflows[best]))

    return Pairing(
        pairs=tuple(pairs),
        matches=tuple(matches),
        unmatched_out=tuple(unmatched_out),
        unmatched_in=tuple(
            entry for index, entry in enumerate(inflows) if index not in claimed
        ),
    )


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
class LedgerEntry:
    source: Path
    account: str
    transaction: Transaction


@dataclass(frozen=True)
class Transfer:
    """Your own money moving between your own accounts."""

    out_source: Path
    out_transaction: Transaction
    in_source: Path
    in_transaction: Transaction

    @property
    def amount(self) -> Paise:
        return self.out_transaction.amount

    @property
    def lag_days(self) -> int:
        return (self.in_transaction.date - self.out_transaction.date).days


@dataclass(frozen=True)
class SpendSummary:
    bank_outflow: Paise
    bank_inflow: Paise
    matched_repayments: Paise
    unmatched_repayments: Paise
    internal_transfers: Paise
    card_charges: Paise

    @property
    def true_outflow(self) -> Paise:
        """What actually left your control.

        Card purchases counted where they happened, the repayments that settle
        them removed, and money shuffled between your own accounts removed from
        both sides. Repayments with no card statement behind them stay in,
        standing in for purchases we cannot see.
        """
        return (
            self.bank_outflow
            - self.matched_repayments
            - self.internal_transfers
            + self.card_charges
        )

    @property
    def true_inflow(self) -> Paise:
        """Income, with the far side of your own transfers taken out."""
        return self.bank_inflow - self.internal_transfers

    @property
    def complete(self) -> bool:
        return self.unmatched_repayments == 0


def merge(statements: Sequence[Statement]) -> tuple[LedgerEntry, ...]:
    """One chronological ledger across accounts, each row tagged with its own."""
    entries = [
        LedgerEntry(
            source=statement.source,
            account=f"{statement.bank} {statement.account_hint}".strip(),
            transaction=txn,
        )
        for statement in statements
        for txn in statement.transactions
    ]
    entries.sort(key=lambda entry: (entry.transaction.date, str(entry.source), entry.transaction.row))
    return tuple(entries)


def find_transfers(
    statements: Sequence[Statement], window: tuple[int, int] = TRANSFER_WINDOW
) -> list[Transfer]:
    """Debits on one account matched to credits on another.

    Reported as candidates. Exact-paise matching across accounts within a few
    days is a strong signal, but paying a friend the same round sum that someone
    else pays you the same day would look identical, so both narrations are
    shown wherever these surface.
    """
    outflows: list[Entry] = []
    inflows: list[Entry] = []
    for statement in statements:
        for txn in statement.transactions:
            target = outflows if txn.direction is Direction.DEBIT else inflows
            target.append((statement.source, txn))
    outflows.sort(key=lambda entry: (entry[1].date, entry[1].row))

    pairing = pair_off(outflows, inflows, window, across_sources_only=True)
    return [
        Transfer(
            out_source=out_source,
            out_transaction=debit,
            in_source=in_source,
            in_transaction=credit,
        )
        for (out_source, debit), (in_source, credit) in pairing.pairs
    ]


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
    repayments: list[Entry] = [
        (statement.source, debit)
        for statement in bank_statements
        for debit in sorted(card_repayments(statement), key=lambda t: (t.date, t.row))
    ]
    payments: list[Entry] = [
        (statement.source, txn)
        for statement in card_statements
        for txn in statement.transactions
        if txn.direction is Direction.CREDIT
    ]

    pairing = pair_off(repayments, payments, window)
    links = [
        RepaymentLink(
            bank_source=bank_source,
            bank_transaction=debit,
            card_source=match[0] if match else None,
            card_transaction=match[1] if match else None,
        )
        for (bank_source, debit), match in zip(repayments, pairing.matches)
    ]
    return Linkage(links=tuple(links), orphan_card_payments=pairing.unmatched_in)


def summarise_spend(
    bank_statements: Sequence[Statement],
    card_statements: Sequence[CardStatement],
    linkage: Linkage | None = None,
    transfers: Sequence[Transfer] | None = None,
) -> SpendSummary:
    linkage = linkage or link_card_repayments(bank_statements, card_statements)
    transfers = (
        transfers if transfers is not None else find_transfers(bank_statements)
    )
    return SpendSummary(
        bank_outflow=_total(bank_statements, Direction.DEBIT),
        bank_inflow=_total(bank_statements, Direction.CREDIT),
        matched_repayments=linkage.matched_total,
        unmatched_repayments=linkage.unmatched_total,
        internal_transfers=sum(transfer.amount for transfer in transfers),
        card_charges=_total(card_statements, Direction.DEBIT),
    )


def _total(statements: Sequence, direction: Direction) -> Paise:
    return sum(
        txn.amount
        for statement in statements
        for txn in statement.transactions
        if txn.direction is direction
    )
