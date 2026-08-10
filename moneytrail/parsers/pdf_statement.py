"""PDF statements -- the format Indian banks actually email you.

Two extraction passes. Most bank statements draw the table, so ruling lines
recover the grid exactly; the ones that do not get a second pass that clusters
text by position. If neither finds a header the file is rejected rather than
guessed at, because a half-recovered table would reconcile against a wrong
total and that is the one outcome this project exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from ..models import Statement
from .base import (
    PasswordRequired,
    StatementParser,
    UnparseableStatement,
    find_account_hint,
)
from .table import RawRow, build_statement, clean_cell, detect_bank, find_header, is_header

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

    def parse(self, path: Path, *, password: str | None = None) -> Statement:
        pdfplumber = _import_pdfplumber()

        try:
            document = pdfplumber.open(path, password=password or "")
        except _password_errors():
            raise PasswordRequired(path, wrong=bool(password)) from None
        except Exception as exc:  # corrupt file, truncated download, not a PDF
            raise UnparseableStatement(f"{path}: could not open PDF -- {exc}") from exc

        with document:
            preamble = (document.pages[0].extract_text() or "").splitlines()
            for settings in (LINE_SETTINGS, TEXT_SETTINGS):
                rows = _extract_rows(document, settings)
                found = find_header([row.cells for row in rows])
                if found is None:
                    continue
                header_index, columns = found
                return build_statement(
                    source=path,
                    bank=detect_bank(preamble, path),
                    account_hint=find_account_hint(preamble),
                    columns=columns,
                    # The header repeats on every page of a multi-page table.
                    rows=[
                        row
                        for row in rows[header_index + 1 :]
                        if is_header(row.cells) is None
                    ],
                )

        raise UnparseableStatement(
            f"{path}: no recognisable transaction table found. If the PDF is a "
            f"scan rather than digital text, it needs OCR first."
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


def _password_errors() -> tuple[type[BaseException], ...]:
    try:
        from pdfminer.pdfdocument import PDFPasswordIncorrect
    except ImportError:
        return ()
    return (PDFPasswordIncorrect,)
