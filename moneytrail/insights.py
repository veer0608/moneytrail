"""Roll a ledger up by merchant.

The number that matters here is not the spend total -- it is **coverage**: what
share of transactions resolved to a merchant the tool is actually confident
about. A normaliser that turns every narration into some plausible-looking
title has 100% output and 0% value, so confident matches are counted
separately from ones that were merely made readable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .merchants import MerchantMatch, build_vocabulary, identify
from .models import Direction, Statement
from .money import Paise


@dataclass(frozen=True)
class MerchantTotals:
    name: str
    category: str
    debits: Paise = 0
    credits: Paise = 0
    count: int = 0
    sources: Counter = field(default_factory=Counter)

    @property
    def turnover(self) -> Paise:
        return self.debits + self.credits

    @property
    def net(self) -> Paise:
        return self.credits - self.debits


@dataclass(frozen=True)
class MerchantRollup:
    entries: tuple[MerchantTotals, ...]
    matches: tuple[MerchantMatch, ...]
    transactions: int

    @property
    def confident(self) -> int:
        return sum(1 for match in self.matches if match.confident)

    @property
    def coverage(self) -> float:
        return self.confident / self.transactions if self.transactions else 0.0

    @property
    def by_source(self) -> Counter:
        return Counter(match.source for match in self.matches)

    @property
    def by_kind(self) -> Counter:
        return Counter(match.kind.value for match in self.matches)

    @property
    def classified(self) -> int:
        return sum(1 for match in self.matches if match.classified)

    @property
    def unclassified(self) -> tuple[MerchantMatch, ...]:
        """The genuinely unknown ones -- the actionable list.

        Not the same as "unnamed": a transfer to a person is correctly
        understood even though no merchant was matched.
        """
        return tuple(match for match in self.matches if not match.classified)


def roll_up(statement: Statement) -> MerchantRollup:
    narrations = [txn.narration for txn in statement.transactions]
    vocabulary = build_vocabulary(narrations)
    matches = [identify(narration, vocabulary) for narration in narrations]

    debits: Counter = Counter()
    credits: Counter = Counter()
    counts: Counter = Counter()
    categories: dict[str, str] = {}
    sources: dict[str, Counter] = {}

    for txn, match in zip(statement.transactions, matches):
        key = match.name
        categories.setdefault(key, match.category)
        sources.setdefault(key, Counter())[match.source] += 1
        counts[key] += 1
        if txn.direction is Direction.DEBIT:
            debits[key] += txn.amount
        else:
            credits[key] += txn.amount

    entries = [
        MerchantTotals(
            name=name,
            category=categories[name],
            debits=debits[name],
            credits=credits[name],
            count=counts[name],
            sources=sources[name],
        )
        for name in counts
    ]
    entries.sort(key=lambda entry: (-entry.turnover, entry.name))

    return MerchantRollup(
        entries=tuple(entries),
        matches=tuple(matches),
        transactions=len(matches),
    )


def by_category(rollup: MerchantRollup) -> list[tuple[str, Paise, Paise, int]]:
    """``(category, debits, credits, count)``, biggest spend first."""
    debits: Counter = Counter()
    credits: Counter = Counter()
    counts: Counter = Counter()
    for entry in rollup.entries:
        debits[entry.category] += entry.debits
        credits[entry.category] += entry.credits
        counts[entry.category] += entry.count

    rows = [
        (category, debits[category], credits[category], counts[category])
        for category in counts
    ]
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows
