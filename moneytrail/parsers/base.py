"""Parser interface plus the date/column guessing every Indian format needs."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import Enum
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from ..models import Statement

class DateOrder(str, Enum):
    """Which of ``03/04`` is the day.

    Undecidable for one date and usually decidable for a statement, which is
    why this is inferred per file rather than configured. Everything about
    ``moneytrail`` is built to fail loudly, and reading an American statement
    day-first is the one failure the reconciliation gate cannot see: the
    arithmetic does not depend on dates, so every check passes and every date
    below the twelfth is silently wrong.
    """

    DAY_FIRST = "day-first"
    MONTH_FIRST = "month-first"


#: The ambiguous ones. Seen in the wild across HDFC / ICICI / SBI / Kotak /
#: Axis exports, and identical in shape to what US banks write.
_DAY_FIRST = ("%d/%m/%y", "%d/%m/%Y", "%d-%m-%y", "%d-%m-%Y")
_MONTH_FIRST = ("%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y")
#: These name the month or lead with the year, so no reading of them is in
#: doubt and they are accepted whichever way round the numeric ones turn out.
_UNAMBIGUOUS = ("%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %B %Y", "%Y-%m-%d")

#: Kept under its old name and its old meaning: the day-first reading.
DATE_FORMATS = _DAY_FIRST + _UNAMBIGUOUS

#: ``03/04/2025`` and ``3-4-25`` -- the shapes that can be read two ways.
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})\s*$")


def formats_for(order: "DateOrder") -> tuple[str, ...]:
    ambiguous = _MONTH_FIRST if order is DateOrder.MONTH_FIRST else _DAY_FIRST
    return ambiguous + _UNAMBIGUOUS

#: A masked account number, e.g. "XXXXXXXX4471" or "****4471". No leading \b --
#: "*" is not a word character, so a boundary would never match the "****" form.
ACCOUNT_HINT = re.compile(r"(?:[Xx]{2,}|\*{2,})\s?\d{3,6}\b")

#: Banks abbreviate freely -- "Closing Balance", "Closing Bal", "Bal C/F".
_OPENING_NARRATIONS = re.compile(
    r"^(opening\s+bal(?:ance)?\.?|bal(?:ance)?\.?\s+b[/\s.]*f|b[/\s.]*f"
    r"|brought\s+forward)\b",
    re.IGNORECASE,
)
_CLOSING_NARRATIONS = re.compile(
    r"^(closing\s+bal(?:ance)?\.?|bal(?:ance)?\.?\s+c[/\s.]*f|c[/\s.]*f"
    r"|carried\s+forward)\b",
    re.IGNORECASE,
)
_SUMMARY_HEADING = re.compile(
    r"^(statement|account|transaction)\s+summary\b|^summary\s*[:\-]",
    re.IGNORECASE,
)


class UnparseableStatement(Exception):
    """The file matched a parser but its contents could not be read."""


class PasswordRequired(UnparseableStatement):
    """The file is encrypted and no usable password was supplied.

    Indian banks email statements locked with something derived from your PAN,
    date of birth or customer ID. The password is never stored or logged by this
    tool -- it is used to open the file and then dropped.
    """

    def __init__(self, path: Path, *, wrong: bool = False) -> None:
        detail = "password is incorrect" if wrong else "password required"
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.wrong = wrong


class NoParserFound(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(f"no parser recognised {path}")
        self.path = path


class StatementParser(ABC):
    name: str
    #: File extensions worth offering to this parser. Used to keep directory
    #: walks from reporting every unrelated file as unreadable.
    suffixes: tuple[str, ...] = ()

    @abstractmethod
    def sniff(self, path: Path) -> bool:
        """Cheap check: could this parser handle the file?"""

    @abstractmethod
    def parse(self, path: Path, *, password: str | None = None) -> Statement:
        """Read the file into a Statement, or raise UnparseableStatement."""


def parse_date(text: str, order: DateOrder = DateOrder.DAY_FIRST) -> date:
    cleaned = text.strip()
    for fmt in formats_for(order):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise UnparseableStatement(f"unrecognised date: {text!r}")


def looks_like_date(text: str) -> bool:
    """Whether this cell is a date under *either* reading.

    Deliberately not order-aware. This decides whether a row is a transaction,
    and that question is settled before the file's date order is known --
    ``12/31/2025`` is a date whatever a statement turns out to mean by
    ``03/04``, and refusing to see it as one would drop the row entirely.
    """
    for order in (DateOrder.DAY_FIRST, DateOrder.MONTH_FIRST):
        try:
            parse_date(text, order)
            return True
        except UnparseableStatement:
            continue
    return False


def infer_date_order(texts: Sequence[str]) -> tuple[DateOrder, bool]:
    """``(order, observed)`` for a statement's dates.

    ``observed`` is False when nothing in the file settled it and the
    day-first default was assumed.

    Two signals, strongest first. A component above twelve cannot be a month,
    so one such date decides the whole file -- and any statement covering more
    than twelve days contains one. Failing that, statements run forwards: if
    only one reading puts the dates in order, it is the reading.

    Both signals at once means the file disagrees with itself, which is a
    parse fault worth raising rather than a preference worth picking.
    """
    pairs: list[tuple[int, int]] = []
    for text in texts:
        found = _NUMERIC_DATE.match(text or "")
        if found:
            pairs.append((int(found.group(1)), int(found.group(2))))

    if not pairs:
        # Nothing ambiguous in the file: every date named its month or led
        # with its year, and the order cannot be got wrong.
        return DateOrder.DAY_FIRST, True

    day_first = any(first > 12 for first, _ in pairs)
    month_first = any(second > 12 for _, second in pairs)

    if day_first and month_first:
        raise UnparseableStatement(
            "dates cannot be read consistently: some put a number above 12 "
            "first and others put one second, so no single order fits the file"
        )
    if day_first:
        return DateOrder.DAY_FIRST, True
    if month_first:
        return DateOrder.MONTH_FIRST, True

    ascending = {
        order: _runs_forwards(texts, order)
        for order in (DateOrder.DAY_FIRST, DateOrder.MONTH_FIRST)
    }
    if ascending[DateOrder.DAY_FIRST] != ascending[DateOrder.MONTH_FIRST]:
        decided = next(o for o, ok in ascending.items() if ok)
        return decided, True

    # Every date at or below the twelfth, and chronological either way. A
    # genuinely undecidable file -- rare, and only on short statements.
    return DateOrder.DAY_FIRST, False


def _runs_forwards(texts: Sequence[str], order: DateOrder) -> bool:
    read: list[date] = []
    for text in texts:
        try:
            read.append(parse_date(text, order))
        except UnparseableStatement:
            continue
    return bool(read) and read == sorted(read)


#: Banks label the money columns with the currency: ``Withdrawal Amount(INR)``,
#: ``Balance (INR)``, ``Deposit Amt in Rs.``. The suffix carries no meaning the
#: parser needs -- amounts are paise either way -- and enumerating every
#: spelling of it in the alias tables is a game that cannot be won: ICICI omits
#: the space before the bracket, and one alias list already carried
#: ``balance (inr)`` *with* one, which matched nothing it was written for.
#:
#: ``(Dr)`` and ``(Cr)`` are deliberately not stripped. Those say which side of
#: the ledger a column is, which is exactly the meaning this is allowed to
#: discard for a currency and must never discard for a direction.
_CURRENCY_SUFFIX = re.compile(
    r"""
    \s*
    (?:
        # (INR), [Rs.], (₹), and the "(in Rs)" ICICI heads its card column with
        [(\[] \s* (?: in \s+ )? (?: inr | rs\.? | ₹ | usd | \$ ) \s* [)\]]
      | \s in \s (?: inr | rs\.? | ₹ )                          # ... in INR
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalise_header(text: str) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation and currency.

    The currency suffix goes last, after the whitespace collapse, so that
    ``Balance (INR)`` and ``Balance(INR)`` reduce to the same thing.
    """
    cleaned = re.sub(r"\s+", " ", text.strip().lower()).rstrip(".:")
    return _CURRENCY_SUFFIX.sub("", cleaned).strip()


def is_opening_row(narration: str) -> bool:
    return bool(_OPENING_NARRATIONS.match(narration.strip()))


def is_closing_row(narration: str) -> bool:
    return bool(_CLOSING_NARRATIONS.match(narration.strip()))


#: Axis prints ``TRANSACTION TOTAL`` under the ledger with its own debit and
#: credit sums. Matched whole-cell rather than as a prefix: "TOTAL" is a common
#: enough word that a prefix match would claim narrations like "TOTAL ENERGIES
#: FUEL", and a misread totals row is worse than an unread one.
_TOTALS_ROW = re.compile(
    r"^(transaction|grand|statement)?\s*totals?(\s+(of\s+)?transactions?)?$",
    re.IGNORECASE,
)


def is_totals_row(text: str) -> bool:
    """True when this cell labels a row of the bank's own column totals."""
    return bool(_TOTALS_ROW.fullmatch(text.strip()))


def is_summary_heading(text: str) -> bool:
    """"STATEMENT SUMMARY :-" and friends: the ledger has ended."""
    return bool(_SUMMARY_HEADING.match(text.strip()))


def find_account_hint(lines: list[str]) -> str:
    for line in lines:
        match = ACCOUNT_HINT.search(line)
        if match:
            return match.group(0).replace(" ", "")
    return ""
