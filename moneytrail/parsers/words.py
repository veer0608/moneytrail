"""Recover a grid from where the words sit, for statements that draw no table.

pdfplumber recovers a ruled table almost for free, and a borderless one not at
all: ``extract_table()`` returns ``None`` when there are no lines to work from,
and its text strategy splits on whitespace, which turns ``HDFC BANK`` into two
columns and a wrapped narration into a row of its own. Most real bank
statements are borderless. Without this module the PDF path only ever read
documents this project generated itself.

The approach is the one the layout actually supports. A statement's columns are
real -- they are simply not drawn -- and the header names them at known
positions. So: cluster words into lines by their vertical position, find the
line that names the columns, take the boundaries from the gaps between those
names, and drop every later word into the column its centre falls in. Amounts
are right-aligned under left-aligned headings, which is why the boundary sits
midway between one heading and the next rather than at either edge.

The hard part is that **a row is not a line**. Banks wrap a long narration over
several lines and put the date and the amounts on whichever of them they like.
Real HDFC statements print the date on the first line and the money on the
second; real ICICI statements centre the dated line inside the narration, so
the text arrives above *and* below the figures. A row therefore starts at a
line carrying a date and absorbs the lines after it, filling any cell it does
not already have.

Narration fragments printed *above* a dated line attach to the transaction
before them rather than the one they belong to. That is a real limitation and
it is deliberate: it can misplace words, never money. Every figure comes from
the line it was printed on and from the column it was printed in, and the
reconciliation gate is what catches it if that is ever wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .base import (
    is_closing_row,
    is_opening_row,
    is_summary_heading,
    looks_like_date,
    normalise_header,
)
from .table import RawRow, match_column

#: Words whose tops differ by less than this are on the same line. Bank
#: statements set 8-10pt type on ~11pt leading, so half a line is a wide
#: margin for the sub-pixel jitter in a PDF's text matrix without ever
#: swallowing the line below.
LINE_TOLERANCE = 3.0

#: Widest gap that can still be a space inside one heading. "Withdrawal Amt."
#: is one heading with a space in it; "Value Dt" and "Withdrawal" are two
#: headings with a column boundary between them.
#:
#: Used as a ceiling rather than as the answer. Measured on a real HDFC header,
#: the word spaces are 1.8pt and the narrowest column gap is 12.7pt -- so a
#: fixed 12 works, with 0.7pt to spare, which is one font substitution away
#: from merging two money columns into one and losing a whole side of the
#: ledger. The threshold is derived per line instead; this only bounds it.
LABEL_GAP = 12.0

#: A column boundary is at least this many times a word space. The two
#: populations are far enough apart on every statement measured that the exact
#: multiple does not matter -- 1.8pt against 12.7pt leaves the whole range from
#: 3x to 7x correct.
GAP_RATIO = 3.0

#: Floor, for a header set so tight that three word spaces is still nothing.
MIN_LABEL_GAP = 4.0


def label_threshold(line: Sequence[dict]) -> float:
    """Where this line's word spaces end and its column gaps begin.

    Derived from the line rather than fixed, because the two populations are
    obvious within a line and not comparable between them: a statement set in
    7pt has narrower spaces *and* narrower column gaps than one set in 10pt,
    and a constant cannot be right for both. Bounded by ``LABEL_GAP`` so this
    is never worse than the fixed threshold it replaced.
    """
    gaps = [
        max(0.0, b["x0"] - a["x1"]) for a, b in zip(line, line[1:])
    ]
    if len(gaps) < 2:
        return LABEL_GAP
    return min(LABEL_GAP, max(MIN_LABEL_GAP, min(gaps) * GAP_RATIO))


@dataclass(frozen=True)
class Column:
    """One recovered column: what it is called, and where it starts and ends."""

    name: str
    label: str
    left: float
    right: float


def cluster_lines(words: Sequence[dict]) -> list[list[dict]]:
    """Group words into visual lines, each sorted left to right."""
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def group_labels(line: Sequence[dict]) -> list[tuple[str, float, float]]:
    """Join adjacent words into ``(text, x0, x1)`` headings."""
    threshold = label_threshold(line)
    labels: list[tuple[list[str], float, float]] = []
    for word in line:
        if labels and word["x0"] - labels[-1][2] <= threshold:
            words, x0, _ = labels[-1]
            labels[-1] = (words + [word["text"]], x0, word["x1"])
        else:
            labels.append(([word["text"]], word["x0"], word["x1"]))
    return [(" ".join(words), x0, x1) for words, x0, x1 in labels]


def read_header(line: Sequence[dict]) -> list[Column] | None:
    """Turn a header line into columns, or ``None`` if it is not one.

    Boundaries land midway between one heading and the next. An amount is
    right-aligned and its heading is not, so neither edge of the heading is
    where the column actually ends -- the empty space between headings is, and
    its middle is the only defensible point in it.
    """
    labels = group_labels(line)
    if len(labels) < 3:
        return None

    named: list[tuple[str, str, float, float]] = []
    taken: set[str] = set()
    for text, x0, x1 in labels:
        field = match_column(normalise_header(text), tuple(taken))
        if field is not None:
            taken.add(field)
        named.append((field or "", text, x0, x1))

    if "date" not in taken:
        return None
    if not ({"balance", "amount"} & taken or {"debit", "credit"} <= taken):
        return None

    columns: list[Column] = []
    for index, (field, text, x0, x1) in enumerate(named):
        left = float("-inf") if index == 0 else (named[index - 1][3] + x0) / 2
        right = float("inf") if index == len(named) - 1 else (x1 + named[index + 1][2]) / 2
        columns.append(Column(name=field, label=text, left=left, right=right))
    return columns


def place(word: dict, columns: Sequence[Column]) -> int | None:
    """Which column this word's centre falls in."""
    centre = (word["x0"] + word["x1"]) / 2
    for index, column in enumerate(columns):
        if column.left <= centre < column.right:
            return index
    return None


def line_to_cells(line: Sequence[dict], columns: Sequence[Column]) -> list[str]:
    """Drop a line's words into the columns they were printed under."""
    cells: list[list[str]] = [[] for _ in columns]
    for word in line:
        index = place(word, columns)
        if index is not None:
            cells[index].append(word["text"])
    return [" ".join(parts) for parts in cells]


def column_map(columns: Sequence[Column]) -> dict[str, int]:
    """``{"date": 0, "narration": 1, ...}`` for the columns that were named."""
    return {c.name: i for i, c in enumerate(columns) if c.name}


def assemble(
    lines: Iterable[Sequence[dict]],
    columns: Sequence[Column],
    *,
    page: int | None = None,
    start: int = 1,
) -> list[RawRow]:
    """Fold lines into rows, a row starting wherever a date appears.

    A line with no date is a continuation: its narration is appended, and any
    cell the row does not yet have is filled from it. That last part is what
    reads a real HDFC statement, where the date is printed on the first line of
    a wrapped row and the amounts on the second.
    """
    date_at = column_map(columns).get("date")
    narration_at = column_map(columns).get("narration")
    rows: list[RawRow] = []
    current: list[str] | None = None
    number = start
    pending: list[str] = []

    for line in lines:
        cells = line_to_cells(line, columns)
        dated = date_at is not None and looks_like_date(cells[date_at])

        if dated:
            if current is not None:
                rows.append(RawRow(number=number, cells=current, page=page))
                number += 1
            current = cells
            # Fragments printed above the first dated row belong to it: there
            # is no earlier transaction for them to have come from.
            if pending and narration_at is not None and not rows:
                current[narration_at] = " ".join(
                    pending + [current[narration_at]]
                ).strip()
            pending = []
            continue

        # A marker line ends the absorbing. Without this the footer prose under
        # the table -- "Closing balance includes funds earmarked for hold" --
        # is swallowed into the last transaction's narration, and the endpoint
        # it announces is never seen by the code that looks for endpoints.
        # Emitted as its own row so that machinery still gets it.
        if any(
            is_opening_row(cell) or is_closing_row(cell) or is_summary_heading(cell)
            for cell in cells
        ):
            if current is not None:
                rows.append(RawRow(number=number, cells=current, page=page))
                number += 1
                current = None
            rows.append(RawRow(number=number, cells=cells, page=page))
            number += 1
            pending = []
            continue

        if current is None:
            # Still above the first transaction -- hold the text, do not invent
            # a row out of it.
            pending.extend(part for part in cells if part)
            continue

        for index, value in enumerate(cells):
            if not value:
                continue
            if index == narration_at or not current[index]:
                current[index] = f"{current[index]} {value}".strip()

    if current is not None:
        rows.append(RawRow(number=number, cells=current, page=page))
    return rows


def recover(
    document,
) -> tuple[list[RawRow], list[RawRow], dict[str, int]] | None:
    """``(rows, grid, columns)`` for a borderless statement, or ``None``.

    ``grid`` is every line on every page, ``rows`` only the assembled
    transactions. Both are needed and they are not the same thing: whether this
    is a card statement is decided by markers like "Total Amount Due", which a
    card prints in a summary box *above* the table. Handing back only the
    transaction rows would hide that box, and a card statement would be built
    as a bank one and then rejected for having no running balance.

    The header repeats on each page of a multi-page statement and is used to
    re-anchor the columns there, because a bank is free to shift them between
    pages and reading page two against page one's geometry would put amounts in
    the wrong column silently.
    """
    columns: list[Column] | None = None
    rows: list[RawRow] = []
    grid: list[RawRow] = []

    for number, page in enumerate(document.pages, start=1):
        lines = cluster_lines(page.extract_words())
        header_at = None
        for index, line in enumerate(lines):
            found = read_header(line)
            if found is not None:
                columns, header_at = found, index
                break

        if columns is None:
            continue  # preamble page, or a cover sheet before the ledger

        grid.extend(
            RawRow(number=i, cells=line_to_cells(line, columns), page=number)
            for i, line in enumerate(lines, start=1)
        )
        body = lines[header_at + 1 :] if header_at is not None else lines
        rows.extend(assemble(body, columns, page=number, start=len(rows) + 1))

    if columns is None:
        return None
    return rows, grid, column_map(columns)
