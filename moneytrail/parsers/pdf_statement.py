"""PDF statements -- the format Indian banks actually email you.

Three extraction passes, in order of how much they know rather than how hard
they try.

Ruling first: where a bank draws its table, the lines state the geometry
exactly and nothing inferred should be allowed to override that. Then
`words.py`, which recovers the columns from where the header's own words sit --
the common case, because most real statements draw nothing. Whitespace
clustering last, for anything whose header the word pass could not name.

That order is load-bearing. pdfplumber's whitespace strategy finds a header
often enough to look like it worked, while splitting "HDFC BANK" across two
columns and losing the running-balance column entirely -- which costs the chain
check and leaves the statement reporting RECONCILED on half the evidence.
Succeeding worse is not succeeding sooner.

If none of the three finds a header the file is rejected rather than guessed
at, because a half-recovered table would reconcile against a wrong total, and
that is the one outcome this project exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from ..models import CardStatement, Statement
from .base import PasswordRequired, StatementParser, UnparseableStatement
from .build import build
from .table import RawRow, clean_cell, find_header, is_header
from .card import summary_patterns
from .words import recover

#: Ruled tables first -- exact when the bank draws borders.
LINE_SETTINGS = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
#: Last resort. Splits on whitespace, which mangles more than it recovers --
#: see the ordering note above for why it runs after the word pass.
TEXT_SETTINGS = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "text_x_tolerance": 2,
}

_INSTALL_HINT = (
    "PDF support needs pdfplumber -- install it with: pip install pdfplumber"
)


class PdfStatementParser(StatementParser):
    name = "pdf"
    suffixes = (".pdf",)

    def sniff(self, path: Path) -> bool:
        return path.suffix.lower() in self.suffixes

    def parse(
        self, path: Path, *, password: str | None = None
    ) -> Statement | CardStatement:
        pdfplumber = _import_pdfplumber()

        try:
            document = pdfplumber.open(path, password=password or "")
        except Exception as exc:  # corrupt file, truncated download, not a PDF
            if _is_password_problem(exc) or is_encrypted(path):
                raise PasswordRequired(path, wrong=bool(password)) from None
            raise UnparseableStatement(f"{path}: could not open PDF -- {exc}") from exc

        with document:
            preamble = (document.pages[0].extract_text() or "").splitlines()
            # Ruling first: a drawn table states its own geometry, and nothing
            # inferred should be allowed to override that.
            rows = _extract_rows(document, LINE_SETTINGS)
            found = find_header([row.cells for row in rows])
            if found is not None:
                header_index, columns = found
                return build(
                    source=path,
                    columns=columns,
                    # The header repeats on every page of a multi-page table.
                    rows=[
                        row
                        for row in rows[header_index + 1 :]
                        if is_header(row.cells) is None
                    ],
                    grid=rows,
                    preamble=preamble,
                )

            # Then word positions, which read the header's own geometry.
            # Ahead of the whitespace strategy deliberately: that one finds a
            # header often enough to look like it worked, while splitting
            # "HDFC BANK" across two columns and dropping the running-balance
            # column entirely -- which costs the chain check and leaves the
            # statement reporting RECONCILED on half the evidence. Succeeding
            # worse is not succeeding sooner.
            recovered = recover(document, summary_patterns())
            if recovered is not None:
                rows, grid, columns, labelled = recovered
                return build(
                    source=path,
                    columns=columns,
                    rows=rows,
                    grid=grid,
                    preamble=preamble,
                    labelled=labelled,
                )

            # Last: pdfplumber's whitespace clustering, for anything whose
            # header the word pass could not name.
            rows = _extract_rows(document, TEXT_SETTINGS)
            found = find_header([row.cells for row in rows])
            if found is not None:
                header_index, columns = found
                return build(
                    source=path,
                    columns=columns,
                    rows=[
                        row
                        for row in rows[header_index + 1 :]
                        if is_header(row.cells) is None
                    ],
                    grid=rows,
                    preamble=preamble,
                )

        # Two very different failures, and telling someone the wrong one sends
        # them off to OCR a document that was never scanned. A page with text
        # on it was read fine; what failed was recognising a table in it.
        if any(line.strip() for line in preamble):
            raise UnparseableStatement(
                f"{path}: the text was read, but no transaction table was "
                f"recognised in it -- no header naming a date column and a "
                f"balance or a debit/credit pair. If this is a statement, the "
                f"header is worded in a way the parser does not know yet."
            )
        raise UnparseableStatement(
            f"{path}: no text at all on the first page, so there is nothing to "
            f"parse. A scanned or photographed statement needs OCR first."
        )


def _extract_rows(document, settings: dict) -> list[RawRow]:
    """Every table row in the document, numbered per page so a human can find it."""
    rows: list[RawRow] = []
    for page_number, page in enumerate(document.pages, start=1):
        row_number = 0
        for table in page.extract_tables(settings) or []:
            for cells in table:
                row_number += 1
                rows.append(
                    RawRow(
                        number=row_number,
                        cells=[clean_cell(cell) for cell in cells],
                        page=page_number,
                    )
                )
    return rows


def _import_pdfplumber():
    try:
        import pdfplumber
    except ImportError as exc:
        raise UnparseableStatement(_INSTALL_HINT) from exc
    return pdfplumber


def is_encrypted(path: Path) -> bool:
    """Whether the PDF declares an /Encrypt entry.

    The authority on "this needs a password" is the file, not an exception
    class. pdfminer's password error has moved between versions and on some
    platforms arrives with an empty message, so a statement that was correctly
    reported as LOCKED on one machine came back as an unreadable file on
    another. Reading the flag out of the document settles it everywhere.
    """
    try:
        return b"/Encrypt" in path.read_bytes()
    except OSError:
        return False


def _is_password_problem(exc: BaseException) -> bool:
    """Match on class name across the cause chain rather than on an import."""
    seen: BaseException | None = exc
    while seen is not None:
        if "password" in type(seen).__name__.lower():
            return True
        seen = seen.__cause__ or seen.__context__
    return False
