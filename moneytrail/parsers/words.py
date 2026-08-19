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
from ..money import parse_optional_amount
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


#: Indian banks embed a rupee font and map the symbol onto an ASCII codepoint,
#: so ``₹1,234.00`` extracts as ``C 1,234.00`` -- HDFC's card statements use
#: ITFRupee, where the glyph drawn at ``C`` is the rupee sign. Read as text the
#: letter is genuinely a ``C``; only the font says otherwise, so the font is
#: what this trusts. Guessing from the letter instead would mean treating a
#: bare "C" before digits as currency everywhere, and a reference number like
#: ``C123456`` would quietly become an amount.
RUPEE_FONTS = ("rupee",)


def read_words(page) -> list[dict]:
    """A page's words, with rupee-font glyphs restored to ``₹``."""
    words = page.extract_words(extra_attrs=["fontname"])
    for word in words:
        font = (word.get("fontname") or "").lower()
        if any(marker in font for marker in RUPEE_FONTS):
            word["text"] = "₹"
    return words


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


#: A line further below its predecessor than this many times the document's own
#: line pitch has been set apart from the table rather than wrapped within it.
#: Measured on a real HDFC statement, rows sit 11pt apart and the footer
#: disclaimer 21pt below the last of them, so the two populations are nearly
#: two-to-one and anything from 1.4 to 1.8 separates them.
BREAK_RATIO = 1.6


def _is_amount(text: str) -> bool:
    """Whether this cell holds a figure, rather than words about figures."""
    try:
        return parse_optional_amount(text) is not None
    except ValueError:
        return False


def line_pitch(lines: Sequence[Sequence[dict]]) -> float | None:
    """The document's own line spacing, as the commonest gap between lines.

    Taken from the statement rather than assumed, for the same reason the label
    threshold is: 7pt type and 10pt type are set on different leading, and a
    constant that separates a wrapped line from a footer on one will not on the
    other. The mode rather than the mean, because a handful of large gaps at
    section breaks would drag an average up past the thing it has to detect.
    """
    tops = [line[0]["top"] for line in lines if line]
    gaps = [round(b - a, 1) for a, b in zip(tops, tops[1:]) if b > a]
    if len(gaps) < 3:
        return None
    return max(set(gaps), key=gaps.count)


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

    lines = list(lines)
    pitch = line_pitch(lines)
    previous_top: float | None = None

    for line in lines:
        cells = line_to_cells(line, columns)
        dated = date_at is not None and looks_like_date(cells[date_at])

        top = line[0]["top"]
        detached = (
            previous_top is not None
            and pitch is not None
            and top - previous_top > pitch * BREAK_RATIO
        )
        previous_top = top

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

        # A marker line, or a line set apart from the table, ends the
        # absorbing. Without this the footer prose under the table is swallowed
        # into the last transaction's narration -- "IB BILLPAY DR-HDFCVE
        # balance includes funds earmarked" is one real statement's last
        # transaction wearing half a disclaimer -- and any endpoint the footer
        # announces is never seen by the code that looks for endpoints.
        #
        # The marker is tested against the whole line rather than each cell:
        # "Closing balance includes ..." starts under the date column and
        # continues under the narration one, so no single cell begins with
        # "closing balance" and a per-cell check misses it entirely.
        joined = " ".join(part for part in cells if part)
        # A balance row states a balance. "Closing balance includes funds
        # earmarked for hold and uncleared funds" is a disclaimer HDFC prints
        # under every page and states nothing, so the label alone is not
        # enough -- without the figure it is prose, and prose is dropped.
        marker = is_summary_heading(joined) or (
            (is_opening_row(joined) or is_closing_row(joined))
            and any(_is_amount(part) for part in cells)
        )
        if marker or detached:
            if current is not None:
                rows.append(RawRow(number=number, cells=current, page=page))
                number += 1
                current = None
            pending = []
            # A marker is handed on, because the endpoints and summary blocks
            # it announces are read downstream. Detached prose is dropped
            # outright rather than passed along: a row with no date and no
            # figures looks exactly like a wrapped narration to the builder,
            # which would glue the page footer onto the last transaction --
            # the thing this branch exists to prevent.
            if marker:
                rows.append(RawRow(number=number, cells=cells, page=page))
                number += 1
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
        lines = cluster_lines(read_words(page))
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
