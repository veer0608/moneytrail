"""PDF statements -- the format Indian banks actually email you.

Two extraction passes. Most bank statements draw the table, so ruling lines
recover the grid exactly; the ones that do not get a second pass that clusters
text by position. If neither finds a header the file is rejected rather than
guessed at, because a half-recovered table would reconcile against a wrong
total and that is the one outcome this project exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from ..models import CardStatement, Statement
from .base import PasswordRequired, StatementParser, UnparseableStatement
from .build import build
from .table import RawRow, clean_cell, find_header, is_header

#: Ruled tables first -- exact when the bank draws borders.
LINE_SETTINGS = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
#: Fallback for borderless layouts: cluster on whitespace instead.
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
            for settings in (LINE_SETTINGS, TEXT_SETTINGS):
                rows = _extract_rows(document, settings)
                found = find_header([row.cells for row in rows])
                if found is None:
                    continue
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

        # Two very different failures, and telling someone the wrong one sends
        # them off to OCR a document that was never scanned. A page with text
        # on it was read fine; what failed was recognising a table in it.
        if any(line.strip() for line in preamble):
            raise UnparseableStatement(
                f"{path}: the text was read, but no transaction table was "
                f"recognised in it. Statements that draw no ruling lines around "
                f"their table are the usual cause -- the columns then have to be "
                f"recovered from where the words sit, which this does not yet do."
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
