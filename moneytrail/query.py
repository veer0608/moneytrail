"""Ask the ledger a question in English and get an answer you can check.

No model runs here, and that is the design rather than a shortcut. The promise
of this project is that every figure is traceable to the rows behind it; a
language model asked to read a ledger and report a total can produce a
confident, plausible, wrong number, and nothing about the output would show it.
So questions are parsed into a structured query and the arithmetic is done by
code, which means every answer arrives carrying the exact rows it came from.

That also leaves the right seam for a model later: let it translate English into
one of these queries, and keep the engine computing the number. The model picks
what to ask; it never gets to decide what the answer is.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from .merchants import MerchantMatch, build_vocabulary, identify
from .models import CardStatement, Direction, Statement, Transaction
from .money import Paise, format_paise
from .patterns import find_duplicates, find_recurring, find_refunds

MONTHS = {
    name.lower(): number
    for number, name in enumerate(calendar.month_name)
    if name
} | {
    name.lower(): number
    for number, name in enumerate(calendar.month_abbr)
    if name
}

_INCOME_WORDS = re.compile(r"\b(receive[d]?|income|earn(ed)?|paid me|credited)\b", re.I)


def plural(count: int, word: str, suffix: str = "s") -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}{suffix}"


@dataclass(frozen=True)
class LedgerRow:
    source: Path
    transaction: Transaction
    match: MerchantMatch
    on_card: bool = False


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str

    def holds(self, when: date) -> bool:
        return self.start <= when <= self.end


@dataclass(frozen=True)
class Answer:
    """A headline, the filters that produced it, and the rows behind it."""

    question: str
    headline: str
    rows: tuple[LedgerRow, ...] = ()
    amount: Paise | None = None
    filters: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    understood: bool = True

    @property
    def evidence(self) -> int:
        return len(self.rows)


@dataclass
class Ledger:
    """Every transaction across every statement, labelled once."""

    rows: tuple[LedgerRow, ...]
    statements: tuple[Statement | CardStatement, ...] = field(default=())

    @property
    def merchants(self) -> set[str]:
        return {row.match.name for row in self.rows}

    @property
    def categories(self) -> set[str]:
        return {row.match.category for row in self.rows}

    @property
    def last_date(self) -> date | None:
        return max((row.transaction.date for row in self.rows), default=None)


def build_ledger(statements: Sequence[Statement | CardStatement]) -> Ledger:
    rows: list[LedgerRow] = []
    for statement in statements:
        narrations = [txn.narration for txn in statement.transactions]
        vocabulary = build_vocabulary(narrations)
        on_card = isinstance(statement, CardStatement)
        for txn in statement.transactions:
            rows.append(
                LedgerRow(
                    source=statement.source,
                    transaction=txn,
                    match=identify(txn.narration, vocabulary),
                    on_card=on_card,
                )
            )
    rows.sort(key=lambda row: (row.transaction.date, str(row.source), row.transaction.row))
    return Ledger(rows=tuple(rows), statements=tuple(statements))


def ask(question: str, ledger: Ledger) -> Answer:
    text = question.lower().strip()
    if not ledger.rows:
        return Answer(question, "no statements loaded", understood=False)

    period = parse_period(text, ledger.last_date)
    merchant = find_merchant(text, ledger)
    category = find_category(text, ledger)

    if re.search(r"\brefund|money back|got it back\b", text):
        return _refunds(question, ledger, merchant, period)
    if re.search(r"\bsubscription|recurring|every month|regular(ly)?\b", text):
        return _recurring(question, ledger)
    if re.search(r"\btwice|duplicate|double[- ]?charg|charged again\b", text):
        return _duplicates(question, ledger)
    if re.search(r"\bbiggest|largest|top\b|\bmost\b", text):
        return _top(question, ledger, period, category, text)
    if re.search(r"\bhow many|how often|how frequently\b", text):
        return _count(question, ledger, merchant, category, period, text)
    if re.search(r"\bhow much|total|spend|spent|pay|paid|paying|cost\b", text):
        return _total(question, ledger, merchant, category, period, text)

    return Answer(
        question,
        "I can answer totals, counts, biggest-spend, refunds, duplicates and "
        "subscriptions. Try: how much did I spend on food in March?",
        understood=False,
    )


# --- filters ---------------------------------------------------------------


def parse_period(text: str, as_of: date | None) -> Period | None:
    """Relative dates resolve against the ledger, never against today.

    A statement from last year should answer "last month" the way it looked
    then, not relative to whenever the question happens to be asked.
    """
    if as_of is None:
        return None

    year_match = re.search(r"\b(20\d\d)\b", text)
    year = int(year_match.group(1)) if year_match else None

    named = _months_named(text, year)
    if named:
        in_year = year or as_of.year
        first, last_month = named[0], named[-1]
        end_day = calendar.monthrange(in_year, last_month)[1]
        label = (
            f"{calendar.month_name[first]} {in_year}"
            if first == last_month
            else f"{calendar.month_name[first]} to {calendar.month_name[last_month]} {in_year}"
        )
        return Period(
            date(in_year, first, 1), date(in_year, last_month, end_day), label
        )

    if re.search(r"\blast month\b", text):
        year_, month = (as_of.year, as_of.month - 1) if as_of.month > 1 else (as_of.year - 1, 12)
        last = calendar.monthrange(year_, month)[1]
        return Period(
            date(year_, month, 1),
            date(year_, month, last),
            f"{calendar.month_name[month]} {year_}",
        )

    if re.search(r"\bthis month\b", text):
        last = calendar.monthrange(as_of.year, as_of.month)[1]
        return Period(
            date(as_of.year, as_of.month, 1),
            date(as_of.year, as_of.month, last),
            f"{calendar.month_name[as_of.month]} {as_of.year}",
        )

    if year is not None:
        return Period(date(year, 1, 1), date(year, 12, 31), str(year))

    return None


#: Month names that are also ordinary English words. "How much may I have
#: spent" is not a question about May.
_AMBIGUOUS_MONTHS = frozenset({"may", "march", "august", "mar", "aug"})
#: "to" and "and" included so the far end of a range is pinned down too.
_DATE_PREPOSITION = r"(?:in|during|for|of|from|since|through|until|till|by|to|and)\s+"


def _months_named(text: str, year: int | None) -> list[int]:
    """Month numbers in the order they appear, so a range stays a range.

    Taking only the first match would answer "January to March" with January
    alone -- a confident answer to a narrower question than the one asked.
    """
    seen: dict[int, int] = {}  # month number -> position in the text
    for name, number in MONTHS.items():
        if not _names_a_month(text, name, year):
            continue
        found = re.search(rf"\b{name}\b", text)
        if found and number not in seen:
            seen[number] = found.start()
    return [number for number, _ in sorted(seen.items(), key=lambda item: item[1])]


def _names_a_month(text: str, name: str, year: int | None) -> bool:
    if name not in _AMBIGUOUS_MONTHS:
        return bool(re.search(rf"\b{name}\b", text))
    # Only a month when a date preposition or a year pins it down.
    if re.search(rf"\b{_DATE_PREPOSITION}{name}\b", text):
        return True
    return year is not None and bool(re.search(rf"\b{name}\b\s*{year}", text))


def find_merchant(text: str, ledger: Ledger) -> str | None:
    """Longest matching merchant name wins, so Swiggy Instamart beats Swiggy."""
    best: str | None = None
    for name in ledger.merchants:
        lowered = name.lower()
        words = lowered.split()
        if not words or lowered.startswith("("):
            continue  # unlabelled rows are not something to match a question to
        hit = lowered in text or (
            len(words[0]) >= 4 and re.search(rf"\b{re.escape(words[0])}\b", text)
        )
        if hit and (best is None or len(name) > len(best)):
            best = name
    return best


_WORD = re.compile(r"[a-z][a-z0-9.&'-]{1,}", re.I)

#: Ordinary question vocabulary. Anything outside this, outside the ledger's own
#: merchants and categories, and outside the calendar, is a name the ledger has
#: never heard of. Erring towards refusal is deliberate: a question wrongly
#: refused is visible and the user rephrases, whereas a question quietly
#: broadened returns a confident answer to something nobody asked.
_ORDINARY_WORDS = frozenset(
    """
    a an and any anything all am are as at average be been biggest bill bills but
    by can card cards cash cost costs could credited date dates day days debited
    did do does doing done each earn earned every everything expense expenses
    find for from get got give given had has have her here him his how i in
    income into is it its largest last least list many may me might money month
    months most much must my myself next nothing of often on or order ordered
    our out paid pay paying please received receive recurring refund refunds
    rs rupees inr send sent shall should show smallest so some something spend
    spending spent statement statements subscription subscriptions such tell than
    that the their them then there these they this those time times to top total
    totals transaction transactions transfer transferred us was we week weeks
    were what when where which who why will with would year years you your
    duplicate duplicates charge charged charges account accounts bank upi
    """.split()
)


def unrecognised(
    text: str, ledger: Ledger, merchant: str | None, category: str | None
) -> tuple[str, ...]:
    """Words the question is about that this ledger has never heard of.

    Without this, "how much did I spend at Tesco in March" quietly drops the
    unknown name and answers for *everything* in March -- a confident answer to
    a question nobody asked, which is the exact failure this project exists to
    avoid.
    """
    known = {word for name in ledger.merchants for word in name.lower().split()}
    known |= {word for name in ledger.categories for word in name.lower().split()}
    if merchant:
        known |= set(merchant.lower().split())
    if category:
        known |= set(category.lower().split())

    unknown: list[str] = []
    for candidate in _WORD.findall(text):
        word = candidate.lower().strip(".,'")
        if word in known or word in _ORDINARY_WORDS or word in MONTHS:
            continue
        if word.isdigit() or word in unknown:
            continue
        unknown.append(word)
    return tuple(unknown)


def find_category(text: str, ledger: Ledger) -> str | None:
    for name in sorted(ledger.categories, key=len, reverse=True):
        if name != "uncategorised" and re.search(rf"\b{re.escape(name)}\b", text):
            return name
    return None


def select(
    ledger: Ledger,
    *,
    merchant: str | None = None,
    category: str | None = None,
    period: Period | None = None,
    direction: Direction | None = None,
    on_card: bool | None = None,
) -> tuple[LedgerRow, ...]:
    return tuple(
        row
        for row in ledger.rows
        if (merchant is None or row.match.name == merchant)
        and (category is None or row.match.category == category)
        and (period is None or period.holds(row.transaction.date))
        and (direction is None or row.transaction.direction is direction)
        and (on_card is None or row.on_card is on_card)
    )


def wants_card_only(text: str, ledger: Ledger) -> bool:
    """"on my card" means the card statement, not everything you own.

    Only honoured when a card statement is actually loaded; otherwise the word
    is just noise and the question is about the bank account.
    """
    return bool(re.search(r"\bcards?\b", text)) and any(
        row.on_card for row in ledger.rows
    )


def _direction_for(text: str) -> Direction:
    return Direction.CREDIT if _INCOME_WORDS.search(text) else Direction.DEBIT


def _describe(merchant, category, period, direction) -> tuple[str, ...]:
    filters = [f"direction={direction.value}"]
    if merchant:
        filters.append(f"merchant={merchant}")
    if category:
        filters.append(f"category={category}")
    filters.append(f"period={period.label if period else 'all statements'}")
    return tuple(filters)


# --- intents ---------------------------------------------------------------


def _refuse_unknown(question, ledger, merchant, category, text) -> Answer | None:
    """Refuse rather than silently answer a broader question."""
    if merchant is not None:
        return None
    strangers = unrecognised(text, ledger, merchant, category)
    if not strangers:
        return None
    named = ", ".join(f'"{word}"' for word in strangers)
    return Answer(
        question,
        f"nothing in these statements matches {named}",
        caveats=(
            f"no merchant or category by that name appears here. Answering "
            f"without it would be a different question, so I have not.",
        ),
        understood=False,
    )


def _total(question, ledger, merchant, category, period, text) -> Answer:
    refusal = _refuse_unknown(question, ledger, merchant, category, text)
    if refusal is not None:
        return refusal
    direction = _direction_for(text)
    on_card = True if wants_card_only(text, ledger) else None
    rows = select(
        ledger,
        merchant=merchant,
        category=category,
        period=period,
        direction=direction,
        on_card=on_card,
    )
    total = sum(row.transaction.amount for row in rows)
    incoming = direction is Direction.CREDIT
    subject = merchant or category
    where = f" in {period.label}" if period else ""
    verb = "received" if incoming else "spent"
    what = f" from {subject}" if (incoming and subject) else (f" on {subject}" if subject else "")

    if not rows:
        return Answer(
            question,
            f"nothing matching{what or ' that'}{where}",
            filters=_describe(merchant, category, period, direction),
            caveats=("no rows matched -- check the merchant or month is in these statements",),
        )
    return Answer(
        question,
        f"{format_paise(total)} {verb}{what}{where}"
        f"{' on the card' if on_card else ''}, "
        f"across {plural(len(rows), 'transaction')}",
        rows=rows,
        amount=total,
        filters=_describe(merchant, category, period, direction)
        + (("source=card statements only",) if on_card else ()),
    )


def _count(question, ledger, merchant, category, period, text) -> Answer:
    refusal = _refuse_unknown(question, ledger, merchant, category, text)
    if refusal is not None:
        return refusal
    direction = _direction_for(text)
    rows = select(
        ledger, merchant=merchant, category=category, period=period, direction=direction
    )
    what = merchant or category or "transactions"
    where = f" in {period.label}" if period else ""
    total = sum(row.transaction.amount for row in rows)
    return Answer(
        question,
        f"{plural(len(rows), 'time')}{where} for {what}, "
        f"{format_paise(total)} in total",
        rows=rows,
        amount=total,
        filters=_describe(merchant, category, period, direction),
    )


def _top(question, ledger, period, category, text) -> Answer:
    refusal = _refuse_unknown(question, ledger, None, category, text)
    if refusal is not None:
        return refusal

    rows = select(ledger, category=category, period=period, direction=Direction.DEBIT)
    if not rows:
        return Answer(question, "nothing matched", caveats=("no rows in that period",))

    totals: dict[str, Paise] = {}
    for row in rows:
        totals[row.match.name] = totals.get(row.match.name, 0) + row.transaction.amount
    ranked = sorted(totals.items(), key=lambda item: -item[1])[:5]

    where = f" in {period.label}" if period else ""
    listing = "; ".join(f"{name} {format_paise(amount)}" for name, amount in ranked)
    named = {name for name, _ in ranked}
    return Answer(
        question,
        f"biggest{where}: {listing}",
        # Evidence has to cover everything the headline claims, not just the
        # first entry, or the answer is only partly checkable.
        rows=tuple(row for row in rows if row.match.name in named),
        amount=ranked[0][1],
        filters=_describe(None, category, period, Direction.DEBIT),
    )


def _refunds(question, ledger, merchant, period) -> Answer:
    loops = [
        loop
        for statement in ledger.statements
        for loop in find_refunds(statement)
        if (merchant is None or loop.merchant == merchant)
        and (period is None or period.holds(loop.refund.date))
    ]
    caveat = (
        "a refund you were owed but never issued cannot be detected -- nothing "
        "in a statement records that you asked for one",
    )
    who = merchant or "any merchant"

    if not loops:
        return Answer(
            question,
            f"no refund from {who} found in these statements",
            filters=(f"merchant={who}",),
            caveats=caveat,
        )

    described = "; ".join(
        f"{loop.merchant} {format_paise(loop.amount)} on {loop.refund.date} "
        f"({loop.lag_days} days after the charge)"
        for loop in loops
    )
    lookup = {id(loop.refund) for loop in loops}
    return Answer(
        question,
        f"yes -- {described}",
        rows=tuple(row for row in ledger.rows if id(row.transaction) in lookup),
        amount=sum(loop.amount for loop in loops),
        filters=(f"merchant={who}",),
        caveats=caveat,
    )


def _recurring(question, ledger) -> Answer:
    items = [item for statement in ledger.statements for item in find_recurring(statement)]
    if not items:
        return Answer(
            question,
            "no recurring charges found",
            caveats=("a cadence needs at least three charges at regular gaps",),
        )
    items.sort(key=lambda item: -item.annualised)
    active = [item for item in items if item.active]
    stopped = len(items) - len(active)
    described = "; ".join(
        f"{item.merchant} {format_paise(item.typical_amount)} {item.cadence}"
        f"{'' if item.active else ' (stopped)'}"
        for item in items
    )
    heading = plural(len(active), "active charge")
    if stopped:
        heading += f", {stopped} stopped"
    return Answer(
        question,
        f"{heading}: {described}",
        amount=sum(item.annualised for item in active),
        filters=("recurring charges, cadence measured from the ledger",),
        caveats=(
            f"active subscriptions cost {format_paise(sum(i.annualised for i in active))} a year "
            "at the current cadence",
        ),
    )


def _duplicates(question, ledger) -> Answer:
    found = [dup for statement in ledger.statements for dup in find_duplicates(statement)]
    if not found:
        return Answer(question, "no duplicate charges found")
    described = "; ".join(
        f"{dup.merchant} {format_paise(dup.amount)} x{dup.count} on {dup.charges[0].date}"
        f" ({format_paise(dup.exposure)} still out)"
        for dup in found
    )
    charge_ids = {id(txn) for dup in found for txn in dup.charges}
    return Answer(
        question,
        f"{len(found)} possible duplicate(s): {described}",
        rows=tuple(row for row in ledger.rows if id(row.transaction) in charge_ids),
        amount=sum(dup.exposure for dup in found),
        caveats=(
            "candidates, not verdicts -- buying the same thing twice looks "
            "identical to being charged twice",
        ),
    )
