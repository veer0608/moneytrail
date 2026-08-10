"""Spreadsheet exports.

Banks routinely hand out a file named ``.xls`` that is actually an OOXML
workbook, so the extension is not trusted -- the magic bytes decide. A genuine
old-format binary ``.xls`` is refused with instructions rather than guessed at.

One honesty note: Excel stores money as a float, so values arrive as floats and
are rounded to two decimals at this boundary. Everything downstream is integer
paise. The rounding happens once, here, where it can be seen.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from ..models import CardStatement, Statement
from .base import StatementParser, UnparseableStatement
from .build import build
from .table import RawRow, find_header, is_header

_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")

_INSTALL_HINT = (
    "Spreadsheet support needs openpyxl -- install it with: pip install openpyxl"
)
_OLD_FORMAT_HINT = (
    "this is a legacy binary .xls, which no maintained Python reader supports. "
    "Open it and re-save as .xlsx or CSV"
)


class XlsxStatementParser(StatementParser):
    name = "xlsx"
    suffixes = (".xlsx", ".xlsm", ".xls")

    def sniff(self, path: Path) -> bool:
        if path.suffix.lower() not in self.suffixes:
            return False
        try:
            with path.open("rb") as handle:
                magic = handle.read(8)
        except OSError:
            return False
        # Claim legacy binaries too, so parse() can explain the problem instead
        # of the file falling through to a bare "no parser recognised".
        return magic[:4] == _ZIP_MAGIC or magic == _OLE2_MAGIC

    def parse(
        self, path: Path, *, password: str | None = None
    ) -> Statement | CardStatement:
        _reject_legacy_format(path)
        openpyxl = _import_openpyxl()

        # Pass a handle, not a path: openpyxl refuses anything named .xls on the
        # extension alone, and banks name OOXML workbooks .xls constantly. The
        # magic bytes were already checked, so the extension carries no
        # information worth honouring.
        with path.open("rb") as handle:
            try:
                workbook = openpyxl.load_workbook(handle, read_only=True, data_only=True)
            except Exception as exc:
                raise UnparseableStatement(
                    f"{path}: could not open workbook -- {exc}"
                ) from exc

            try:
                for sheet in workbook.worksheets:
                    grid = [
                        [_cell_text(value) for value in row]
                        for row in sheet.iter_rows(values_only=True)
                    ]
                    found = find_header(grid)
                    if found is None:
                        continue
                    header_index, columns = found
                    numbered = [
                        RawRow(number=index + 1, cells=cells)
                        for index, cells in enumerate(grid)
                    ]
                    return build(
                        source=path,
                        columns=columns,
                        rows=[
                            row
                            for row in numbered[header_index + 1 :]
                            if is_header(row.cells) is None
                        ],
                        grid=numbered,
                        preamble=[" ".join(cells) for cells in grid[:header_index]],
                    )
            finally:
                workbook.close()

        raise UnparseableStatement(
            f"{path}: no recognisable transaction table in any sheet"
        )


def _cell_text(value: object) -> str:
    """One cell as the text a parser would have seen in a CSV export."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Excel money is a float; pin it to paise here and never again.
        return str(int(value)) if value.is_integer() else f"{value:.2f}"
    return " ".join(str(value).split())


def _reject_legacy_format(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            magic = handle.read(8)
    except OSError as exc:
        raise UnparseableStatement(f"{path}: could not read -- {exc}") from exc
    if magic == _OLE2_MAGIC:
        raise UnparseableStatement(f"{path}: {_OLD_FORMAT_HINT}")


def _import_openpyxl():
    try:
        import openpyxl
    except ImportError as exc:
        raise UnparseableStatement(_INSTALL_HINT) from exc
    return openpyxl
