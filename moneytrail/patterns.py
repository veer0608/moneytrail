"""The questions no banking app will answer.

Three things are genuinely detectable from a clean ledger, and it matters to be
precise about which:

- **refund loops** -- a charge and a later credit from the same merchant for the
  same amount. This answers "did my refund arrive?" for a purchase you can point
  at. It cannot find refunds you were owed and never chased, because nothing in
  a statement records that you asked.
- **duplicate charges** -- the same merchant billing the same amount twice in a
  few days. Inherently a heuristic: buying the same coffee twice looks identical
  in a ledger. So these are reported as candidates with their evidence and their
  span, not as verdicts.
- **recurring charges** -- a merchant billing at a steady cadence. Cadence is
  measured, not assumed, and a run whose intervals are irregular is not called
  recurring however many times it appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Iterable, Sequence

from .merchants import MerchantMatch, build_vocabulary, identify
from .models import CardStatement, Direction, Statement, Transaction
from .money import Paise

#: Two charges this close together, same merchant and amount, look duplicated.
DUPLICATE_WINDOW_DAYS = 3
#: How long after a charge a credit may still be its refund.
REFUND_WINDOW_DAYS = 90
#: Fewest occurrences before a cadence claim is worth making.
MIN_OCCURRENCES = 3
#: Cadences recognised, in days.
KNOWN_CADENCES = {7: "weekly", 14: "fortnightly", 30: "monthly", 91: "quarterly", 365: "annual"}


@dataclass(frozen=True)
class RefundLoop:
    merchant: str
    amount: Paise
    charge: Transaction
    refund: Transaction

    @property
    def lag_days(self) -> int:
        return (self.refund.date - self.charge.date).days


@dataclass(frozen=True)
class DuplicateCharge:
    merchant: str
    amount: Paise
    charges: tuple[Transaction, ...]
    refunded: int  # how many of them were later credited back

    @property
    def count(self) -> int:
        return len(self.charges)

    @property
    def span_days(self) -> int:
        return (self.charges[-1].date - self.charges[0].date).days

    @property
    def exposure(self) -> Paise:
        """What is still out, if the extras were never refunded."""
        return self.amount * max(0, self.count - 1 - self.refunded)


@dataclass(frozen=True)
class RecurringCharge:
    merchant: str
    category: str
    occurrences: tuple[Transaction, ...]
    interval_days: int
    typical_amount: Paise
    active: bool

    @property
    def cadence(self) -> str:
        for days, name in KNOWN_CADENCES.items():
            if abs(self.interval_days - days) <= max(3, days * 0.2):
                return name
        return f"every {self.interval_days}d"

    @property
    def last_seen(self) -> date:
        return self.occurrences[-1].date

    @property
    def annualised(self) -> Paise:
        return round(self.typical_amount * 365 / self.interval_days)


def label(statement: Statement | CardStatement) -> list[tuple[Transaction, MerchantMatch]]:
    """Pair every transaction with its resolved counterparty, once."""
    narrations = [txn.narration for txn in statement.transactions]
    vocabulary = build_vocabulary(narrations)
    return [
        (txn, identify(txn.narration, vocabulary))
        for txn in statement.transactions
    ]


def find_refunds(
    statement: Statement | CardStatement, window_days: int = REFUND_WINDOW_DAYS
) -> list[RefundLoop]:
    """Credits matched back to the charge they reverse, one-to-one."""
    labelled = label(statement)
    charges = _by_merchant(labelled, Direction.DEBIT)
    claimed: set[int] = set()
    loops: list[RefundLoop] = []

    for credit, match in labelled:
        if credit.direction is not Direction.CREDIT:
            continue
        for candidate in charges.get(match.name, []):
            if id(candidate) in claimed or candidate.amount != credit.amount:
                continue
            lag = (credit.date - candidate.date).days
            if not 0 <= lag <= window_days:
                continue
            claimed.add(id(candidate))
            loops.append(
                RefundLoop(
                    merchant=match.name,
                    amount=credit.amount,
                    charge=candidate,
                    refund=credit,
                )
            )
            break

    loops.sort(key=lambda loop: loop.refund.date)
    return loops


def find_duplicates(
    statement: Statement | CardStatement, window_days: int = DUPLICATE_WINDOW_DAYS
) -> list[DuplicateCharge]:
    labelled = label(statement)
    refunded_charges = {id(loop.charge) for loop in find_refunds(statement)}

    groups: dict[tuple[str, Paise], list[Transaction]] = {}
    for txn, match in labelled:
        if txn.direction is Direction.DEBIT:
            groups.setdefault((match.name, txn.amount), []).append(txn)

    duplicates: list[DuplicateCharge] = []
    for (merchant, amount), charges in groups.items():
        for cluster in _cluster_by_date(charges, window_days):
            if len(cluster) < 2:
                continue
            duplicates.append(
                DuplicateCharge(
                    merchant=merchant,
                    amount=amount,
                    charges=tuple(cluster),
                    refunded=sum(1 for txn in cluster if id(txn) in refunded_charges),
                )
            )

    duplicates.sort(key=lambda dup: (-dup.exposure, dup.charges[0].date))
    return duplicates


def find_recurring(
    statement: Statement | CardStatement, minimum: int = MIN_OCCURRENCES
) -> list[RecurringCharge]:
    labelled = label(statement)
    as_of = max((txn.date for txn, _ in labelled), default=None)
    if as_of is None:
        return []

    groups: dict[str, tuple[list[Transaction], MerchantMatch]] = {}
    for txn, match in labelled:
        if txn.direction is not Direction.DEBIT:
            continue
        bucket, _ = groups.setdefault(match.name, ([], match))
        bucket.append(txn)

    recurring: list[RecurringCharge] = []
    for merchant, (charges, match) in groups.items():
        charges.sort(key=lambda txn: (txn.date, txn.row))
        # Same-day repeats are duplicates, not a cadence; collapse them first.
        dates = sorted({txn.date for txn in charges})
        if len(dates) < minimum:
            continue
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        spacing = round(median(intervals))
        if spacing <= 0 or not _regular(intervals, spacing):
            continue
        if not _similar_amounts([txn.amount for txn in charges]):
            continue
        recurring.append(
            RecurringCharge(
                merchant=merchant,
                category=match.category,
                occurrences=tuple(charges),
                interval_days=spacing,
                typical_amount=round(median([txn.amount for txn in charges])),
                active=(as_of - dates[-1]) <= timedelta(days=spacing * 1.5),
            )
        )

    recurring.sort(key=lambda item: (-item.annualised, item.merchant))
    return recurring


def _by_merchant(
    labelled: Sequence[tuple[Transaction, MerchantMatch]], direction: Direction
) -> dict[str, list[Transaction]]:
    grouped: dict[str, list[Transaction]] = {}
    for txn, match in labelled:
        if txn.direction is direction:
            grouped.setdefault(match.name, []).append(txn)
    for charges in grouped.values():
        charges.sort(key=lambda txn: (txn.date, txn.row))
    return grouped


def _cluster_by_date(
    charges: Iterable[Transaction], window_days: int
) -> list[list[Transaction]]:
    ordered = sorted(charges, key=lambda txn: (txn.date, txn.row))
    clusters: list[list[Transaction]] = []
    for txn in ordered:
        if clusters and (txn.date - clusters[-1][-1].date).days <= window_days:
            clusters[-1].append(txn)
        else:
            clusters.append([txn])
    return clusters


def _regular(intervals: Sequence[int], spacing: int) -> bool:
    """Every gap close to the median. One wild gap means this is not a cadence."""
    tolerance = max(4, spacing * 0.25)
    return all(abs(interval - spacing) <= tolerance for interval in intervals)


def _similar_amounts(amounts: Sequence[Paise]) -> bool:
    """Subscriptions creep in price; they do not swing wildly."""
    smallest, largest = min(amounts), max(amounts)
    return largest <= smallest * 1.25 if smallest else largest == 0
